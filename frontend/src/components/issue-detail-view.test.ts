import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './issue-detail-view';
import type { IssueDetailView } from './issue-detail-view';

function makePair() {
  return {
    issue1: {
      id: 'i1',
      key: 'PRJ-1',
      title: 'First',
      status: 'opened',
      description: 'One',
      url: 'https://example.com/1',
    },
    issue2: {
      id: 'i2',
      key: 'PRJ-2',
      title: 'Second',
      status: 'opened',
      description: 'Two',
      url: 'https://example.com/2',
    },
    similarity: 0.9,
  };
}

describe('IssueDetailView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('does not fetch when parent provides state', async () => {
    const el = (await fixture(html`
      <issue-detail-view
        .pair=${makePair()}
        .verdictState=${{
          state: 'done',
          verdict: { decision: 'duplicate', reason: 'same' },
        }}
      ></issue-detail-view>
    `)) as IssueDetailView;
    await el.updateComplete;
    const checkCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/issue-duplicates/check')
      );
    expect(checkCalls.length).to.equal(0);
    expect(el.shadowRoot?.textContent).to.contain('same');
  });

  it('renders no_model copy with Models link', async () => {
    const el = (await fixture(html`
      <issue-detail-view
        .pair=${makePair()}
        .verdictState=${{ state: 'no_model' }}
      ></issue-detail-view>
    `)) as IssueDetailView;
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).to.contain(
      'No AI model configured. Set a default model under'
    );
    const link = el.shadowRoot?.querySelector(
      'a[href="/console/ai-models"]'
    ) as HTMLAnchorElement;
    expect(link).to.exist;
    expect(link.textContent).to.contain('Models');
  });
});
