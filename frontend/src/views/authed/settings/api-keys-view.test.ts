import { expect, fixture, html, nextFrame, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './api-keys-view.ts';
import type { ApiKeysView } from './api-keys-view';
import { unifiedWebSocketManager } from '../../../services/unified-websocket-manager';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { bulkActionButton, bulkCountText } from '../../../utils/test-bulk-bar';

describe('ApiKeysView', () => {
  let fetchStub: sinon.SinonStub;
  let wsStub: sinon.SinonStub;

  beforeEach(() => {
    wsStub = sinon.stub(unifiedWebSocketManager, 'send').returns(true);
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();

        if (
          url.includes('/api/v1/auth/api-keys') &&
          !url.includes('/governance') &&
          (!init || !init.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify([
              {
                id: 'key-1',
                name: 'OpenClaw Managed Key',
                created_at: '2026-03-10T09:00:00Z',
                last_used_at: '2026-03-10T09:45:00Z',
                last_activity_at: '2026-03-10T10:00:00Z',
                activity_status: 'recently_active',
                expires_at: null,
                recent_model_calls: 2,
                recent_tool_calls: 1,
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.includes('/api/v1/auth/api-keys/key-1/governance')) {
          return new Response(
            JSON.stringify({
              subject_type: 'api_keys',
              subject_id: 'key-1',
              config: {
                allowed_models: ['openai/gpt-5'],
                model_budgets: {
                  'openai/gpt-5': { monthly_usd_limit: 10 },
                },
                tool_rules: {
                  search_issues: [{ action: 'require_approval' }],
                },
              },
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/tools')) {
          return new Response(
            JSON.stringify([
              {
                name: 'search_issues',
                description: 'Search GitHub issues',
                schema: { properties: { query: { type: 'string' } } },
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/approval-workflows')) {
          return new Response(
            JSON.stringify([
              {
                id: 'wf-1',
                name: 'Default Approval',
                approval_type: 'standard',
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }

        if (url.endsWith('/api/v1/features')) {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(
          JSON.stringify({ detail: `Unhandled request: ${url}` }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        );
      }
    );
  });

  afterEach(() => {
    wsStub.restore();
    fetchStub.restore();
    localStorage.clear();
  });

  it('loads keys without fetching catalogs, including when creating a key', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );
    await waitUntil(() => !(element as any).isLoading);
    await element.updateComplete;
    const urls = () => fetchStub.getCalls().map((call) => String(call.args[0]));
    expect(
      urls().some((url) => /tools|approval-workflows|features/.test(url))
    ).to.equal(false);
    const createButton = element.shadowRoot!.querySelector(
      'view-header sl-button'
    ) as HTMLElement;
    createButton.click();
    await element.updateComplete;
    expect((element as any).isCreateModalOpen).to.equal(true);
    expect(
      urls().some((url) => /tools|approval-workflows|features/.test(url))
    ).to.equal(false);
  });

  it('loads full tool schemas only when the legacy governance entry opens', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );
    await waitUntil(() => !(element as any).isLoading);
    await (element as any).openGovernanceDialog({ id: 'key-1', name: 'Key' });
    expect((element as any).toolCatalog[0].schema).to.deep.equal({
      properties: { query: { type: 'string' } },
    });
    expect((element as any).approvalWorkflows[0].id).to.equal('wf-1');
    expect((element as any).governanceAllowedModels).to.equal('openai/gpt-5');
    const toolCalls = fetchStub
      .getCalls()
      .filter((call) => String(call.args[0]).includes('/api/v1/tools'));
    expect(toolCalls.length).to.equal(1);
    expect(String(toolCalls[0].args[0])).to.equal('/api/v1/tools');
  });

  it('renders activity status', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    let content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('OpenClaw Managed Key');
    expect(content).to.contain('Recently active');
    expect(content).to.contain('2 model');
    expect(content).to.contain('1 tool');
  });

  it('keeps recently active chips primary so they do not look idle', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'recent-key',
              name: 'Recent Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T09:45:00Z',
              activity_status: 'recently_active',
              expires_at: null,
            },
            {
              id: 'live-key',
              name: 'Live Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T10:00:00Z',
              activity_status: 'active_now',
              expires_at: null,
            },
            {
              id: 'idle-key',
              name: 'Idle Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Revoked Key',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    (element as any).showAllKeys = true;
    await element.updateComplete;

    const chipVariants = Object.fromEntries(
      Array.from(
        element.shadowRoot?.querySelectorAll('sl-badge.chip') || []
      ).map((badge) => [
        badge.textContent?.trim(),
        badge.getAttribute('variant'),
      ])
    );
    expect(chipVariants).to.deep.equal({
      'Recently active': 'primary',
      'Active now': 'success',
      Idle: 'neutral',
      Revoked: 'danger',
    });
  });

  it('hides revoked and expired keys behind a footer and reveals them on Show all', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'live-key',
              name: 'Production Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Flow Execution 389da654',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
            {
              id: 'expired-key',
              name: 'Old Laptop Key',
              created_at: '2025-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: '2025-06-01T09:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    let content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Production Key');
    expect(content).to.not.contain('Flow Execution 389da654');
    expect(content).to.not.contain('Old Laptop Key');
    expect(content).to.contain('2 keys are revoked or expired and hidden');

    const showAll = Array.from(
      element.shadowRoot?.querySelectorAll('.link-button') || []
    ).find((button) => button.textContent?.trim() === 'Show all');
    expect(showAll, 'Show all control is rendered').to.exist;

    (showAll as HTMLButtonElement).click();
    await element.updateComplete;

    content = element.shadowRoot?.textContent || '';
    expect(content).to.contain('Flow Execution 389da654');
    expect(content).to.contain('Old Laptop Key');
    expect(content).to.contain('Revoked');
    expect(content).to.contain('Expired');
  });

  it('offers Revoke only on keys that can still be used', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'live-key',
              name: 'Production Key',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
            {
              id: 'revoked-key',
              name: 'Flow Execution 389da654',
              created_at: '2026-03-01T09:00:00Z',
              last_used_at: null,
              activity_status: 'revoked',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    (element as any).showAllKeys = true;
    await element.updateComplete;

    // No standalone danger button survives: the action lives in a kebab.
    expect(element.shadowRoot?.querySelector('sl-button[variant="danger"]')).to
      .not.exist;

    const actionLists = Array.from(
      element.shadowRoot?.querySelectorAll('resource-actions') || []
    ).map((element_) => (element_ as any).actions as { id: string }[]);
    expect(actionLists.length).to.equal(2);
    expect(actionLists[0].map((action) => action.id)).to.deep.equal(['revoke']);
    expect(actionLists[1]).to.deep.equal([]);
  });

  it('revokes every selected key from the bulk bar after listing them', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (
          url.includes('/api/v1/auth/api-keys') &&
          !url.includes('/governance') &&
          (!init || !init.method || init.method === 'GET')
        ) {
          return new Response(
            JSON.stringify([
              {
                id: 'key-a',
                name: 'Build Key',
                created_at: '2026-03-10T09:00:00Z',
                last_used_at: null,
                activity_status: 'idle',
                expires_at: null,
              },
              {
                id: 'key-b',
                name: 'Deploy Key',
                created_at: '2026-03-10T09:00:00Z',
                last_used_at: null,
                activity_status: 'idle',
                expires_at: null,
              },
              {
                id: 'key-c',
                name: 'Laptop Key',
                created_at: '2026-03-10T09:00:00Z',
                last_used_at: null,
                activity_status: 'idle',
                expires_at: null,
              },
            ]),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          );
        }
        if (url.endsWith('/api/v1/features')) {
          return new Response(JSON.stringify({ features: {} }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('{}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
    );

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );
    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    // Nothing selected: the bar is over the table's header row from the
    // first paint, hidden, so ticking a box costs the table no height.
    const idleBar = element.shadowRoot!.querySelector('list-bulk-bar')!;
    expect(getComputedStyle(idleBar).visibility).to.equal('hidden');

    // Pick the first row, then shift-extend to the third.
    element.selection.toggle('key-a');
    element.selection.toggle('key-c', true);
    await element.updateComplete;
    const bar = element.shadowRoot!.querySelector('list-bulk-bar')!;
    expect(Array.from(element.selection.selectedIds)).to.deep.equal([
      'key-a',
      'key-b',
      'key-c',
    ]);
    expect(bulkCountText(bar)).to.contain('3 selected');
    expect(
      element
        .shadowRoot!.querySelector('tr[data-selection-id="key-b"]')!
        .getAttribute('aria-selected')
    ).to.equal('true');

    const revokeButton = (await bulkActionButton(bar, 'revoke'))!;
    revokeButton.click();

    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'no confirm dialog'
    );
    const dialog = document.querySelector('confirm-dialog')!;
    await (dialog as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const dialogText = dialog.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(dialogText).to.contain('Revoke 3 keys?');
    expect(dialogText).to.contain('Build Key, Deploy Key, Laptop Key');

    const deletes = () =>
      fetchStub
        .getCalls()
        .filter((call) => (call.args[1] as RequestInit)?.method === 'DELETE');
    expect(deletes().length, 'revoked before confirming').to.equal(0);

    const confirmButton = dialog.shadowRoot!.querySelector<HTMLElement>(
      '[data-testid="confirm-dialog-confirm"]'
    )!;
    await (confirmButton as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    confirmButton.click();

    await waitUntil(() => deletes().length === 3, 'not every key was revoked');
    expect(
      deletes()
        .map((call) => String(call.args[0]))
        .sort()
    ).to.deep.equal([
      '/api/v1/auth/api-keys/key-a',
      '/api/v1/auth/api-keys/key-b',
      '/api/v1/auth/api-keys/key-c',
    ]);
    resetConfirmDialogForTests();
  });

  it('takes over the table header instead of pushing the rows down', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );
    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    // This page has no list-toolbar, so the bar takes the header row: the
    // fallback path. The body must not move an inch either way.
    expect(element.shadowRoot!.querySelector('list-toolbar')).to.equal(null);
    const bodyTop = () =>
      element.shadowRoot!.querySelector('tbody')!.getBoundingClientRect().top;
    const before = bodyTop();

    element.selection.toggle('key-1');
    await element.updateComplete;
    await nextFrame();

    const bar = element.shadowRoot!.querySelector('list-bulk-bar')!;
    expect(bulkCountText(bar)).to.contain('1 selected');
    expect(getComputedStyle(bar).visibility).to.equal('visible');
    const headerCell = element.shadowRoot!.querySelector(
      'thead th:last-child'
    )!;
    expect(
      getComputedStyle(headerCell).visibility,
      'the column names stay behind the bar'
    ).to.equal('hidden');
    expect(bodyTop(), 'selecting pushed the key rows down').to.equal(before);

    // Escape gives the header back, at the same place.
    element.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
    );
    await element.updateComplete;
    await nextFrame();
    expect(element.selection.count).to.equal(0);
    expect(getComputedStyle(bar).visibility).to.equal('hidden');
    expect(getComputedStyle(headerCell).visibility).to.equal('visible');
    expect(bodyTop(), 'clearing moved the key rows').to.equal(before);
  });

  it('anchors the bar to a block every engine positions against', async () => {
    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );
    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    element.selection.toggle('key-1');
    await element.updateComplete;
    await nextFrame();

    // WebKit does not treat a positioned table section as a containing block,
    // so a bar anchored to the thead escapes to the viewport there. It hangs
    // off a plain wrapper around the table instead.
    const overlay =
      element.shadowRoot!.querySelector<HTMLElement>('.head-bulk-bar')!;
    expect(overlay.closest('table'), 'the bar sits outside the table').to.equal(
      null
    );
    expect(
      (overlay.offsetParent as HTMLElement | null)?.classList.contains(
        'table-shell'
      ),
      'the wrapper is what positions the bar'
    ).to.equal(true);

    // And it still covers the header row it stands in for.
    const head = element
      .shadowRoot!.querySelector('thead')!
      .getBoundingClientRect();
    const bar = overlay.getBoundingClientRect();
    expect(Math.round(bar.top)).to.equal(Math.round(head.top));
    expect(Math.round(bar.width)).to.equal(Math.round(head.width));
  });

  it('renders Agent badge when managed_agent_id is present', async () => {
    fetchStub.restore();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (
        url.includes('/api/v1/auth/api-keys') &&
        !url.includes('/governance')
      ) {
        return new Response(
          JSON.stringify([
            {
              id: 'managed-key-1',
              name: 'OpenClaw Managed Key',
              managed_agent_id: 'agent-123',
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: '2026-03-10T09:45:00Z',
              activity_status: 'recently_active',
              expires_at: null,
            },
            {
              id: 'user-key-1',
              name: 'Personal Dev Key',
              managed_agent_id: null,
              created_at: '2026-03-10T09:00:00Z',
              last_used_at: null,
              activity_status: 'idle',
              expires_at: null,
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        );
      }
      if (url.endsWith('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const element = await fixture<ApiKeysView>(
      html`<api-keys-view></api-keys-view>`
    );

    await waitUntil(
      () => !(element as any).isLoading,
      'API keys view did not finish loading'
    );
    await element.updateComplete;

    const badges = Array.from(
      element.shadowRoot?.querySelectorAll('sl-badge') || []
    );
    const badgeTexts = badges.map((b) => b.textContent?.trim());
    expect(badgeTexts).to.include('Agent');
  });

  describe('one-time key dialog', () => {
    const openKeyDialog = async () => {
      const element = await fixture<ApiKeysView>(
        html`<api-keys-view></api-keys-view>`
      );
      await waitUntil(() => !(element as any).isLoading);
      (element as any).newlyCreatedKey = {
        id: 'key-new',
        name: 'CI key',
        key: 'pl_example_secret_value',
        created_at: '2026-03-10T09:00:00Z',
      };
      (element as any).isShowKeyModalOpen = true;
      await element.updateComplete;
      const dialog = element.shadowRoot!.querySelector(
        'sl-dialog[label="API key created"]'
      ) as HTMLElement;
      return { element, dialog };
    };

    const requestClose = (dialog: HTMLElement, source: string) => {
      const event = new CustomEvent('sl-request-close', {
        cancelable: true,
        detail: { source },
      });
      dialog.dispatchEvent(event);
      return event.defaultPrevented;
    };

    afterEach(() => {
      sinon.restore();
      delete (navigator as any).clipboard;
    });

    it('stays open on Esc and overlay clicks until the user confirms', async () => {
      const { element, dialog } = await openKeyDialog();
      expect(requestClose(dialog, 'keyboard')).to.equal(true);
      expect(requestClose(dialog, 'overlay')).to.equal(true);
      expect(requestClose(dialog, 'close-button')).to.equal(false);
      const done = dialog.querySelector(
        'sl-button[slot="footer"]'
      ) as HTMLElement;
      expect(done.textContent?.trim()).to.equal('I have copied my key');
      done.click();
      await element.updateComplete;
      expect((element as any).isShowKeyModalOpen).to.equal(false);
    });

    it('shows the key in a read-only field with a usage hint', async () => {
      const { dialog } = await openKeyDialog();
      const input = dialog.querySelector('sl-input.key-field') as any;
      expect(input).to.exist;
      expect(input.readonly).to.equal(true);
      expect(input.value).to.equal('pl_example_secret_value');
      expect(input.getAttribute('label')).to.equal('API key');
      expect(dialog.querySelector('.usage-hint')?.textContent).to.include(
        'Authorization: Bearer'
      );
    });

    it('copies with a labelled button and announces success', async () => {
      const writeText = sinon.stub().resolves();
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText },
      });
      const { element, dialog } = await openKeyDialog();
      const button = dialog.querySelector('sl-button.copy-key') as HTMLElement;
      expect(button.textContent?.trim()).to.equal('Copy key');
      button.click();
      await waitUntil(() => (element as any).keyCopyStatus === 'copied');
      await element.updateComplete;
      expect(writeText.calledOnceWith('pl_example_secret_value')).to.equal(
        true
      );
      expect(button.textContent?.trim()).to.equal('Copied');
      expect(dialog.querySelector('[role="status"]')?.textContent).to.include(
        'API key copied'
      );
    });

    it('falls back to selecting the key when the clipboard API is missing', async () => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: undefined,
      });
      sinon.stub(document, 'execCommand').returns(false);
      const { element, dialog } = await openKeyDialog();
      expect(dialog.textContent).to.include('press Ctrl+C (Cmd+C on Mac)');
      const input = dialog.querySelector('sl-input.key-field') as any;
      const select = sinon.spy(input, 'select');
      (dialog.querySelector('sl-button.copy-key') as HTMLElement).click();
      await waitUntil(() => (element as any).keyCopyStatus === 'manual');
      await element.updateComplete;
      expect(select.called).to.equal(true);
      expect(dialog.querySelector('[role="status"]')?.textContent).to.include(
        'press Ctrl+C'
      );

      input.dispatchEvent(new Event('sl-focus'));
      expect(select.callCount).to.be.greaterThan(1);
    });
  });
});
