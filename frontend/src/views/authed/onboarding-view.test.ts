import { html, fixture, expect } from '@open-wc/testing';

import '../../components/view-header.ts';
import './onboarding-view';
import type { OnboardingView } from './onboarding-view';

describe('OnboardingView', () => {
  it('renders the hero and CLI tab by default', async () => {
    const element = (await fixture(
      html`<onboarding-view></onboarding-view>`
    )) as OnboardingView;
    await element.updateComplete;

    expect((element as any).activeTab).to.equal('cli');
    expect(element.shadowRoot?.textContent).to.contain('Initialize Workforce');
    expect(element.shadowRoot?.textContent).to.contain(
      'npm install -g @preloop/cli'
    );
  });

  it('switches to the OpenClaw plugin tab', async () => {
    const element = (await fixture(
      html`<onboarding-view></onboarding-view>`
    )) as OnboardingView;
    await element.updateComplete;

    (element as any).activeTab = 'plugin';
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('OpenClaw Integration');
    expect(element.shadowRoot?.textContent).to.not.contain(
      'npm install -g @preloop/cli'
    );
  });

  it('switches to the API gateway tab', async () => {
    const element = (await fixture(
      html`<onboarding-view></onboarding-view>`
    )) as OnboardingView;
    await element.updateComplete;

    (element as any).activeTab = 'gateway';
    await element.updateComplete;

    expect(element.shadowRoot?.textContent).to.contain('Gateway API Keys');
    expect(element.shadowRoot?.textContent).to.contain('Security Protocol');
  });

  it('changes the active tab when a nav button is clicked', async () => {
    const element = (await fixture(
      html`<onboarding-view></onboarding-view>`
    )) as OnboardingView;
    await element.updateComplete;

    const buttons = element.shadowRoot?.querySelectorAll('button');
    // The second nav button is the OpenClaw plugin tab.
    (buttons?.[1] as HTMLButtonElement)?.click();
    await element.updateComplete;

    expect((element as any).activeTab).to.equal('plugin');
  });

  it('renders the developer docs link', async () => {
    const element = (await fixture(
      html`<onboarding-view></onboarding-view>`
    )) as OnboardingView;
    await element.updateComplete;

    const docsLink = element.shadowRoot?.querySelector(
      'a[href="https://docs.preloop.ai"]'
    );
    expect(docsLink).to.exist;
  });
});
