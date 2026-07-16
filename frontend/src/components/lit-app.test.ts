import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { Router } from '@vaadin/router';

import './lit-app';

describe('LitApp routing', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = {
      name: 'Preloop',
      domain: 'preloop.ai',
      company: { legal_name: 'Preloop', address: '', city: '' },
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
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      // Endpoints that expect list payloads
      if (
        url.includes('/api/v1/tools') ||
        url.includes('/api/v1/trackers') ||
        url.includes('/api/v1/ai-models') ||
        url.includes('/api/v1/mcp-servers')
      ) {
        return new Response('[]', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    delete (window as any).BRAND_CONFIG;
    window.history.replaceState({}, '', '/');
  });

  it('redirects the legacy /console/onboarding route to /console/agents', async () => {
    await fixture(html`<lit-app></lit-app>`);

    Router.go('/console/onboarding');

    await waitUntil(
      () => window.location.pathname === '/console/agents',
      'Expected /console/onboarding to redirect to /console/agents',
      { timeout: 5000 }
    );

    expect(window.location.pathname).to.equal('/console/agents');
    expect(document.querySelector('onboarding-view')).to.equal(null);
  });
});
