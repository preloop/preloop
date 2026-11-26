import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  waitUntil,
} from '@open-wc/testing';
import sinon from 'sinon';
import './execution-refinalize';

describe('stored review recovery', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'test-access-token'));
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });

  it('shows no action before a qualifying preview', async () => {
    sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify({ eligible: false })));
    const view = await fixture<any>(
      html`<execution-refinalize .executionId=${'e1'}></execution-refinalize>`
    );
    await waitUntil(() => view.preview !== null);
    expect(view.shadowRoot.querySelector('sl-button')).to.equal(null);
  });

  it('previews by default and applies the displayed digest', async () => {
    const requests: any[] = [];
    sinon.stub(window, 'fetch').callsFake(async (url, options) => {
      if (!String(url).endsWith('/refinalize')) return new Response('');
      requests.push(JSON.parse(options?.body as string));
      return new Response(
        JSON.stringify(
          requests.length === 1
            ? {
                eligible: true,
                digest: 'a'.repeat(64),
                selected_result: { verdict: 'pass' },
                gates: { schema: { passed: true } },
                publication: { would_publish: false, reason: 'head_moved' },
              }
            : {
                outcome: 'refinalized',
                refinalization: {
                  at: '2026-01-01T00:00:00Z',
                  user_name: 'Jane Doe',
                },
                publication: { reason: 'head_moved' },
              }
        )
      );
    });
    const view = await fixture<any>(
      html`<execution-refinalize .executionId=${'e1'}></execution-refinalize>`
    );
    await waitUntil(() => !!view.shadowRoot.querySelector('sl-button'));
    expect(view.shadowRoot.textContent).to.contain('newer commit');
    expect(requests[0]).to.deep.equal({ dry_run: true });
    await view.apply();
    await view.updateComplete;
    expect(requests[1]).to.deep.equal({
      dry_run: false,
      expected_digest: 'a'.repeat(64),
    });
    expect(view.shadowRoot.textContent).to.contain('Jane Doe');
    expect(view.shadowRoot.querySelector('sl-button')).to.equal(null);
  });
});
