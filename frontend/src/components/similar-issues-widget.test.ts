import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './similar-issues-widget';
import type { SimilarIssuesWidget } from './similar-issues-widget';

const tick = (ms = 200) => new Promise((r) => setTimeout(r, ms));

describe('SimilarIssuesWidget', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders single no-model line', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), { status: 200 });
        if (url.includes('/api/v1/issue-duplicates/ai-status')) {
          return json({ configured: false, model_name: null });
        }
        if (url.includes('/api/v1/issue-duplicates/check')) {
          return json({ decision: 'duplicate' });
        }
        if (url.includes('/api/v1/issue-duplicates')) {
          return json({
            duplicates: [
              {
                issue1: { id: 'a', key: 'A-1', title: 'A' },
                issue2: { id: 'b', key: 'B-1', title: 'B' },
                similarity: 0.8,
              },
              {
                issue1: { id: 'c', key: 'C-1', title: 'C' },
                issue2: { id: 'd', key: 'D-1', title: 'D' },
                similarity: 0.7,
              },
            ],
          });
        }
        return json({});
      });

    const el = (await fixture(
      html`<similar-issues-widget></similar-issues-widget>`
    )) as SimilarIssuesWidget;
    await tick(300);
    await el.updateComplete;
    const checkCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/issue-duplicates/check')
      );
    expect(checkCalls.length).to.equal(0);
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('AI review needs a default model');
    expect(text.match(/Failed to load/g)).to.equal(null);
    expect(el.shadowRoot?.querySelectorAll('.no-model-line').length).to.equal(
      1
    );
  });
});
