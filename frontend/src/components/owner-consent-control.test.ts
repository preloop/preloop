import { html, fixture, expect } from '@open-wc/testing';
import './owner-consent-control';
import type { OwnerConsentControl } from './owner-consent-control';
import {
  FLOW_CONSENT_OPTIONS,
  SESSION_CONSENT_OPTIONS,
} from '../utils/owner-consent';

describe('owner-consent-control', () => {
  it('lets the owner pick and reports the choice', async () => {
    const el = await fixture<OwnerConsentControl>(
      html`<owner-consent-control
        label="Flow runs on this runner"
        value="ask_me"
        .options=${FLOW_CONSENT_OPTIONS}
        editable
      ></owner-consent-control>`
    );
    const select = el.shadowRoot!.querySelector('sl-select')!;
    expect(select.getAttribute('aria-label')).to.equal(
      'Flow runs on this runner'
    );
    expect(el.shadowRoot!.querySelector('.help')!.textContent).to.contain(
      'approval request before a flow runs'
    );
    let picked = '';
    el.addEventListener('consent-change', (event) => {
      picked = (event as CustomEvent<{ value: string }>).detail.value;
    });
    select.value = 'never';
    select.dispatchEvent(new Event('sl-change'));
    expect(picked).to.equal('never');
  });

  it('shows everyone else the value and who can change it', async () => {
    const el = await fixture<OwnerConsentControl>(
      html`<owner-consent-control
        label="Sessions started by others"
        value="deny"
        .options=${SESSION_CONSENT_OPTIONS}
        ownerOnly="Only the agent owner can change this."
      ></owner-consent-control>`
    );
    expect(el.shadowRoot!.querySelector('sl-select')).to.equal(null);
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain("Don't allow");
    expect(text).to.contain('Only the agent owner can change this.');
  });

  it('falls back to the default option for an unknown value', async () => {
    const el = await fixture<OwnerConsentControl>(
      html`<owner-consent-control
        value=""
        .options=${SESSION_CONSENT_OPTIONS}
      ></owner-consent-control>`
    );
    expect(el.shadowRoot!.textContent).to.contain('Ask me first');
  });
});
