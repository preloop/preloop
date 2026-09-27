import { expect, fixture, html, oneEvent } from '@open-wc/testing';
import sinon from 'sinon';
import './improve-compliance-modal.ts';
import type { ImproveComplianceModal } from './improve-compliance-modal';
import type { Issue } from '../types';

const ISSUE: Issue = {
  id: 'issue-1',
  key: 'EX-1',
  title: 'Original title',
  description: 'Original description',
  status: 'open',
  status_id: 'status-1',
  priority: 'medium',
  priority_id: 'priority-1',
  project_id: 'project-1',
  project_name: 'Example Project',
  organization_id: 'org-1',
  organization_name: 'Example Org',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  source: 'github',
  url: 'https://example.com/issues/1',
};

/** Answer the modal's two API calls; record the URLs it asked for. */
function stubFetch(): { restore: () => void; urls: string[] } {
  const original = window.fetch;
  const urls: string[] = [];
  window.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    urls.push(url);
    if (url.includes('/issue_compliance_suggestion/')) {
      return new Response(
        JSON.stringify({
          title: 'Suggested title',
          description: 'Suggested description',
          changes: 'Clarified scope',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    }
    return new Response(
      JSON.stringify({ id: 'issue-1', title: 'Suggested title' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
  }) as typeof window.fetch;
  return { restore: () => (window.fetch = original), urls };
}

describe('improve-compliance-modal', () => {
  let fetchStub: ReturnType<typeof stubFetch>;
  let logSpy: sinon.SinonSpy;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = stubFetch();
    logSpy = sinon.spy(console, 'log');
  });

  afterEach(() => {
    logSpy.restore();
    fetchStub.restore();
    localStorage.removeItem('accessToken');
  });

  it('submits the suggestion without logging issue data or the response', async () => {
    const el = await fixture<ImproveComplianceModal>(
      html`<improve-compliance-modal
        .issue=${ISSUE}
        promptName="default"
      ></improve-compliance-modal>`
    );
    const internals = el as unknown as {
      _suggestedTitle: string;
      _suggestedDescription: string;
      _handleSubmit: () => Promise<void>;
    };
    internals._suggestedTitle = 'Suggested title';
    internals._suggestedDescription = 'Suggested description';

    const submitted = oneEvent(el, 'on-submit');
    await internals._handleSubmit();
    const event = (await submitted) as CustomEvent;

    expect(event.detail).to.deep.equal({
      issueId: 'issue-1',
      summary: 'Issue EX-1 was successfully updated.',
    });
    expect(
      fetchStub.urls.some((url) =>
        url.includes('/api/v1/issue_compliance_update/issue-1')
      )
    ).to.equal(true);
    expect(logSpy.called).to.equal(false);
  });
});
