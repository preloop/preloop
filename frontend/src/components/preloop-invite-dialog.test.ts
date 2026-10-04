import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './preloop-invite-dialog';
import type { PreloopInviteDialog } from './preloop-invite-dialog';

describe('PreloopInviteDialog', () => {
  let fetchStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(opts: { failFor?: string[] } = {}) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/api/v1/teams')) {
          return json({ teams: [], total: 0 });
        }
        if (url.includes('/api/v1/roles')) {
          return json({
            roles: [
              {
                id: 'role-1',
                name: 'team_admin',
                permissions: ['create_flows'],
              },
            ],
          });
        }
        if (url.includes('/api/v1/invitations') && method === 'POST') {
          const body = JSON.parse(String(init?.body || '{}'));
          if (opts.failFor?.includes(body.email)) {
            return json({ detail: 'User is already a member' }, 400);
          }
          return json({ id: `inv-${body.email}`, email: body.email });
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const posts = () =>
    fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes('/api/v1/invitations') &&
          call.args[1]?.method === 'POST'
      )
      .map((call) => JSON.parse(String(call.args[1].body)).email);

  async function mount(): Promise<PreloopInviteDialog> {
    const element = await fixture<PreloopInviteDialog>(
      html`<preloop-invite-dialog open></preloop-invite-dialog>`
    );
    await waitUntil(() => (element as any).roles.length === 1, 'roles load');
    await element.updateComplete;
    return element;
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('labels roles by their display names', async () => {
    fetchStub = createFetchStub();
    const element = await mount();
    const option = element.shadowRoot!.querySelector('sl-option');
    expect(option?.textContent?.trim()).to.equal('Team admin');
  });

  it('reports addresses that are not emails instead of dropping them', async () => {
    fetchStub = createFetchStub();
    const element = await mount();
    (element as any).emailsText = 'jane@example.com, bob';
    (element as any).selectedRoleIds = ['role-1'];
    await (element as any).handleSendInvitations();
    await element.updateComplete;

    expect(posts()).to.have.length(0);
    const alert = element.shadowRoot!.querySelector('sl-alert[role="alert"]');
    expect(alert?.textContent).to.contain('bob');
    expect(alert?.textContent).to.contain('not an email address');
  });

  it('reports each failed address and keeps only those for a retry', async () => {
    fetchStub = createFetchStub({ failFor: ['john@example.com'] });
    const element = await mount();
    const sent = sinon.spy();
    element.addEventListener('invitations-sent', sent);
    (element as any).emailsText =
      'jane@example.com\njohn@example.com\nalex@example.com';
    (element as any).selectedRoleIds = ['role-1'];
    await (element as any).handleSendInvitations();
    await element.updateComplete;

    // Every address was attempted, even after one failed.
    expect(posts()).to.have.members([
      'jane@example.com',
      'john@example.com',
      'alex@example.com',
    ]);
    const alert = element.shadowRoot!.querySelector('sl-alert[role="alert"]');
    expect(alert?.textContent).to.contain('Sent 2 · 1 failed');
    expect(alert?.textContent).to.contain(
      'john@example.com: User is already a member'
    );
    expect((element as any).emailsText).to.equal('john@example.com');
    // The list behind the dialog refreshes for the two that went out.
    expect(sent.calledOnce).to.equal(true);
    expect((element as any).successMessage).to.equal(null);
  });

  it('confirms a fully successful send', async () => {
    fetchStub = createFetchStub();
    const element = await mount();
    (element as any).emailsText = 'jane@example.com, jane@example.com';
    (element as any).selectedRoleIds = ['role-1'];
    await (element as any).handleSendInvitations();
    await element.updateComplete;

    // The duplicate is invited once.
    expect(posts()).to.deep.equal(['jane@example.com']);
    expect((element as any).successMessage).to.equal('Sent 1 invitation.');
  });
});
