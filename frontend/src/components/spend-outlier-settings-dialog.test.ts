import { expect, fixture, html, oneEvent, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './spend-outlier-settings-dialog.ts';
import {
  parseSpendOutlierForm,
  type SpendOutlierSettingsDialog,
} from './spend-outlier-settings-dialog';

const SETTINGS = {
  daily_multiple: 3,
  min_history_days: 7,
  top_tier_model_prefixes: ['top-model'],
  top_tier_share: 0.5,
  session_cost_threshold_usd: null,
  configured: false,
};

describe('parseSpendOutlierForm', () => {
  const form = {
    dailyMultiple: '3',
    minHistoryDays: '7',
    prefixes: 'top-model\n, other-model ',
    sharePercent: '50',
    sessionThreshold: '',
  };

  it('turns the form into a payload with the session rule off', () => {
    expect(parseSpendOutlierForm(form)).to.eql({
      daily_multiple: 3,
      min_history_days: 7,
      top_tier_model_prefixes: ['top-model', 'other-model'],
      top_tier_share: 0.5,
      session_cost_threshold_usd: null,
    });
  });

  it('reads a session threshold in dollars', () => {
    const parsed = parseSpendOutlierForm({ ...form, sessionThreshold: '25' });
    expect(
      typeof parsed === 'string' ? parsed : parsed.session_cost_threshold_usd
    ).to.equal(25);
  });

  it('names the field that is out of range', () => {
    expect(parseSpendOutlierForm({ ...form, dailyMultiple: '0.5' })).to.contain(
      'daily multiple'
    );
    expect(parseSpendOutlierForm({ ...form, minHistoryDays: '40' })).to.contain(
      'History days'
    );
    expect(parseSpendOutlierForm({ ...form, sharePercent: '100' })).to.contain(
      'top-tier share'
    );
    expect(
      parseSpendOutlierForm({ ...form, sessionThreshold: '-1' })
    ).to.contain('session threshold');
  });
});

describe('spend-outlier-settings-dialog', () => {
  let fetchStub: sinon.SinonStub;
  let writes: any[];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
    writes = [];
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith('/api/v1/attention/spend-outliers/settings')) {
          if ((init?.method || 'GET').toUpperCase() === 'PUT') {
            const body = JSON.parse(String(init!.body));
            writes.push(body);
            return new Response(JSON.stringify({ ...body, configured: true }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            });
          }
          return new Response(JSON.stringify(SETTINGS), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response('{}', { status: 404 });
      });
  });

  afterEach(() => {
    fetchStub.restore();
    invalidateApiCaches();
    localStorage.clear();
  });

  it('loads the settings when opened and saves the edited form', async () => {
    const element = await fixture<SpendOutlierSettingsDialog>(
      html`<spend-outlier-settings-dialog open></spend-outlier-settings-dialog>`
    );
    await waitUntil(
      () => element.shadowRoot!.querySelector('form'),
      'settings form rendered'
    );
    const threshold = element.shadowRoot!.querySelector(
      'sl-input[name="session-threshold"]'
    ) as HTMLInputElement;
    expect(threshold.value).to.equal('');

    threshold.value = '25';
    threshold.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;

    const changed = oneEvent(element, 'spend-outlier-settings-changed');
    element
      .shadowRoot!.querySelector('form')!
      .dispatchEvent(new Event('submit', { cancelable: true }));
    await changed;

    expect(writes).to.have.length(1);
    expect(writes[0].session_cost_threshold_usd).to.equal(25);
    expect(writes[0].top_tier_model_prefixes).to.eql(['top-model']);
  });

  it('shows the first invalid field instead of saving', async () => {
    const element = await fixture<SpendOutlierSettingsDialog>(
      html`<spend-outlier-settings-dialog open></spend-outlier-settings-dialog>`
    );
    await waitUntil(() => element.shadowRoot!.querySelector('form'));
    const multiple = element.shadowRoot!.querySelector(
      'sl-input[name="daily-multiple"]'
    ) as HTMLInputElement;
    multiple.value = '0';
    multiple.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;

    element
      .shadowRoot!.querySelector('form')!
      .dispatchEvent(new Event('submit', { cancelable: true }));
    await element.updateComplete;

    expect(writes).to.have.length(0);
    expect(
      element.shadowRoot!.querySelector('[role="alert"]')!.textContent
    ).to.contain('daily multiple');
  });
});
