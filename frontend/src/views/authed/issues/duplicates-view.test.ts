import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './duplicates-view';
import { DuplicatesView } from './duplicates-view';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

describe('DuplicatesView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  function stub(issues: unknown[]) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(
        async () => new Response(JSON.stringify(issues), { status: 200 })
      );
  }

  it('shows an info alert when there are no duplicate issues', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<duplicates-view></duplicates-view>`
    )) as DuplicatesView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('sl-alert')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('No duplicate issues found');
  });

  it('renders a menu of duplicate issues when present', async () => {
    fetchStub = stub([{ title: 'Login bug' }, { title: 'Auth bug' }]);
    const el = (await fixture(
      html`<duplicates-view></duplicates-view>`
    )) as DuplicatesView;
    await tick();
    await el.updateComplete;
    const items = el.shadowRoot?.querySelectorAll('sl-menu-item');
    expect(items?.length).to.equal(2);
    expect(el.shadowRoot?.textContent).to.contain('Login bug');
  });

  it('requests the duplicate issues endpoint', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<duplicates-view></duplicates-view>`
    )) as DuplicatesView;
    await tick();
    await el.updateComplete;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/api/v1/issue-duplicates'))
    ).to.be.true;
  });
});
