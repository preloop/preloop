import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { ReadinessPolicySettings } from './readiness-policy-settings';
import './readiness-policy-settings';

describe('configured readiness policy settings', () => {
  afterEach(() => sinon.restore());

  it('requires explicit keys and approvals, preserving explicit zero and empty set', async () => {
    const requests: RequestInit[] = [];
    sinon
      .stub(ReadinessPolicySettings.prototype as any, 'fetchData')
      .callsFake(async (_url: string, options: RequestInit = {}) => {
        if (options.method === 'PUT') {
          requests.push(options);
          return { version: 'new-version' };
        }
        return null;
      });
    const element = await fixture<ReadinessPolicySettings>(
      html`<readiness-policy-settings
        project-id="synthetic-project"
      ></readiness-policy-settings>`
    );
    const form = element.shadowRoot!.querySelector('form')!;
    form.dispatchEvent(new SubmitEvent('submit', { cancelable: true }));
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.include(
      'Enter required build keys'
    );
    const inputs = element.shadowRoot!.querySelectorAll('input');
    inputs[0].value = '[]';
    inputs[0].dispatchEvent(new Event('input'));
    inputs[1].value = '0';
    inputs[1].dispatchEvent(new Event('input'));
    form.dispatchEvent(new SubmitEvent('submit', { cancelable: true }));
    await waitUntil(() => requests.length === 1);
    const body = JSON.parse(requests[0].body as string);
    expect(body.required_build_keys).to.deep.equal([]);
    expect(body.minimum_approvals).to.equal(0);
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.include(
      'Forge restrictions remain unknown'
    );
  });

  it('shows unconfigured policy as unknown', async () => {
    sinon
      .stub(ReadinessPolicySettings.prototype as any, 'fetchData')
      .resolves(null);
    const element = await fixture<ReadinessPolicySettings>(
      html`<readiness-policy-settings
        project-id="synthetic-project"
      ></readiness-policy-settings>`
    );
    expect(element.shadowRoot!.textContent).to.include(
      'Unconfigured: ticket readiness is unknown.'
    );
  });
});
