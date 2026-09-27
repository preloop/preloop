import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import './resolve-issue-modal';
import type { ResolveIssueModal } from './resolve-issue-modal';

describe('ResolveIssueModal errors', () => {
  afterEach(() => {
    document.body.querySelectorAll('sl-alert').forEach((node) => {
      node.remove();
    });
    localStorage.clear();
  });

  it('shows a toast when resolution fails', async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    const fetchStub = sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ detail: 'Resolution rejected' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const element = document.createElement(
      'resolve-issue-modal'
    ) as ResolveIssueModal;
    (element as any).duplicatePair = {
      issue1: { id: 'a', key: 'EX-1' },
      issue2: { id: 'b', key: 'EX-2' },
    };

    try {
      await (element as any).handleFinalResolve('UNRELATED');
      const alert = document.body.querySelector('sl-alert');
      expect(alert?.textContent).to.contain('Resolution rejected');
      expect((element as any)._isSubmitting).to.equal(false);
    } finally {
      fetchStub.restore();
    }
  });
});
