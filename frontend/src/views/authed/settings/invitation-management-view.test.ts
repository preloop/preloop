import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';
import './invitation-management-view';
import type { InvitationManagementView } from './invitation-management-view';

describe('InvitationManagementView', () => {
  let fetchStub: sinon.SinonStub;

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(
    opts: {
      featureEnabled?: boolean;
      invitations?: unknown[];
      invitationsFail?: boolean;
    } = {}
  ) {
    const featureEnabled = opts.featureEnabled !== false;
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        if (url.includes('/api/v1/features')) {
          return json({
            plugins: [],
            features: { user_management: featureEnabled },
          });
        }

        if (url.includes('/api/v1/invitations') && method === 'GET') {
          if (opts.invitationsFail) {
            return json({ detail: 'boom' }, 500);
          }
          return json({
            invitations: opts.invitations ?? [],
            total: (opts.invitations ?? []).length,
          });
        }

        if (url.includes('/api/v1/invitations/') && method === 'DELETE') {
          return new Response(null, { status: 204 });
        }

        if (url.includes('/api/v1/teams')) {
          return json({ teams: [], total: 0 });
        }

        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  const sampleInvitation = {
    id: 'inv-1',
    email: 'invitee@example.com',
    status: 'pending',
    created_at: '2026-06-01T10:00:00Z',
    expires_at: '2026-06-08T10:00:00Z',
    accepted_at: null,
  };

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
    resetConfirmDialogForTests();
  });

  it('shows the not-available message when feature is disabled', async () => {
    fetchStub = createFetchStub({ featureEnabled: false });
    const element = (await fixture(
      html`<invitation-management-view></invitation-management-view>`
    )) as InvitationManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain(
      'not available in this edition'
    );
  });

  it('renders the empty state when there are no invitations', async () => {
    fetchStub = createFetchStub({ invitations: [] });
    const element = (await fixture(
      html`<invitation-management-view></invitation-management-view>`
    )) as InvitationManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');
    await element.updateComplete;

    // The page is called what the sidebar calls it, in the shared header.
    expect(
      (element.shadowRoot?.querySelector('view-header') as any)?.headerText
    ).to.equal('Invitations');
    expect(element.shadowRoot?.textContent).to.contain('No invitations found');
  });

  it('renders an invitation when one exists', async () => {
    fetchStub = createFetchStub({ invitations: [sampleInvitation] });
    const element = (await fixture(
      html`<invitation-management-view></invitation-management-view>`
    )) as InvitationManagementView;

    await waitUntil(
      () => (element as any).invitations?.length === 1,
      'invitations did not load'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('invitee@example.com');
    // Status is a tint chip with a human label, not a raw enum value.
    const chip = element.shadowRoot?.querySelector(
      '.invitation-meta sl-badge.status-chip'
    );
    expect(chip?.textContent?.trim()).to.equal('Pending');
  });

  it('shows an error when invitation loading fails', async () => {
    fetchStub = createFetchStub({ invitationsFail: true });
    const element = (await fixture(
      html`<invitation-management-view></invitation-management-view>`
    )) as InvitationManagementView;

    await waitUntil(
      () => (element as any).error !== null,
      'error did not appear'
    );
    await element.updateComplete;

    expect(element.shadowRoot?.querySelector('.error')).to.exist;
  });

  it('reloads invitations when switching tabs', async () => {
    fetchStub = createFetchStub({ invitations: [] });
    const element = (await fixture(
      html`<invitation-management-view></invitation-management-view>`
    )) as InvitationManagementView;

    await waitUntil(() => !(element as any).isLoading, 'still loading');

    const before = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('/api/v1/invitations')).length;

    (element as any).activeTab = 'accepted';
    await (element as any).fetchInvitations();
    await element.updateComplete;

    const after = fetchStub
      .getCalls()
      .filter((c) => String(c.args[0]).includes('/api/v1/invitations')).length;
    expect(after).to.be.greaterThan(before);
    const acceptedCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).includes('status=accepted'));
    expect(acceptedCall, 'expected a request filtered by status=accepted').to
      .exist;
  });

  it('asks before cancelling an invitation', async () => {
    fetchStub = createFetchStub({ invitations: [sampleInvitation] });
    const nativeConfirm = sinon.stub(window, 'confirm');
    try {
      const element = (await fixture(
        html`<invitation-management-view></invitation-management-view>`
      )) as InvitationManagementView;
      await waitUntil(() => (element as any).invitations?.length === 1);
      await element.updateComplete;
      const deletes = () =>
        fetchStub
          .getCalls()
          .filter((call) => call.args[1]?.method === 'DELETE');
      const cancel = element.shadowRoot!.querySelector(
        '.invitation-actions sl-button[variant="danger"]'
      ) as HTMLElement;
      expect(cancel.querySelector('sl-icon')?.getAttribute('label')).to.equal(
        'Cancel invitation'
      );

      cancel.click();
      const prompt = await answerConfirmDialog(false);
      expect(prompt).to.contain('invitee@example.com');
      expect(prompt).to.contain('stops working');
      expect(deletes()).to.have.length(0);

      cancel.click();
      await answerConfirmDialog(true);
      await waitUntil(() => deletes().length === 1);
      expect(nativeConfirm.called).to.equal(false);
    } finally {
      nativeConfirm.restore();
    }
  });
});
