import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './request-demo-view';
import { RequestDemoView } from './request-demo-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
  branding: {
    logo_light: '/logo.svg',
    logo_dark: '/logo-dark.svg',
    favicon: '/favicon.ico',
    primary_color: '#000',
    gradient_product: '',
    gradient_ai: '',
  },
  social: { twitter: '', linkedin: '', instagram: '' },
};

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

describe('RequestDemoView', () => {
  let el: RequestDemoView;
  let fetchStub: sinon.SinonStub;

  beforeEach(async () => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
    fetchStub = sinon.stub(window, 'fetch');
    el = (await fixture(
      html`<request-demo-view></request-demo-view>`
    )) as RequestDemoView;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('renders the demo request form with tracker checkboxes', () => {
    expect(el.shadowRoot?.querySelector('form')).to.exist;
    expect(el.shadowRoot?.querySelector('sl-select')).to.exist;
    const checkboxes = el.shadowRoot?.querySelectorAll('sl-checkbox');
    expect(checkboxes?.length).to.equal(4);
  });

  it('tracks tracker selection via checkbox change', () => {
    (el as any)._handleTrackerChange({
      target: { checked: true, value: 'GitHub' },
    } as unknown as CustomEvent);
    expect((el as any)._trackers).to.deep.equal(['GitHub']);
    (el as any)._handleTrackerChange({
      target: { checked: false, value: 'GitHub' },
    } as unknown as CustomEvent);
    expect((el as any)._trackers).to.deep.equal([]);
  });

  it('submits the lead and shows a thank-you message on success', async () => {
    fetchStub.callsFake(
      async () => new Response(JSON.stringify({}), { status: 200 })
    );
    (el as any)._name = 'Jane';
    (el as any)._email = 'jane@example.com';
    await (el as any)._handleSubmit(new Event('submit'));
    await tick();
    await el.updateComplete;
    expect((el as any)._success).to.be.true;
    expect(el.shadowRoot?.textContent).to.contain('Thank you');
    expect(
      fetchStub.getCalls().some((c) => String(c.args[0]).includes('/leads'))
    ).to.be.true;
  });

  it('marks the form as submitting while the request is in flight', () => {
    fetchStub.callsFake(
      () =>
        new Promise(() => {
          /* never resolves */
        })
    );
    void (el as any)._handleSubmit(new Event('submit'));
    expect((el as any)._submitting).to.be.true;
  });
});
