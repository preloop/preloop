import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import './weekly-report-settings';
import type { WeeklyReportSettings } from './weekly-report-settings';

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubApi(options: {
  capability?: boolean;
  permissions?: string[] | null;
  prefs?: Record<string, boolean>;
}) {
  const prefs = options.prefs ?? {
    account_enabled: true,
    user_enabled: true,
    members_enabled: true,
    member_policy_findings_enabled: false,
  };
  const puts: Record<string, unknown>[] = [];
  const stub = sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      const path = new URL(url, window.location.origin).pathname;
      if (path === '/api/v1/features')
        return json({
          features: { governance_review: options.capability ?? true },
        });
      if (path === '/api/v1/auth/users/me')
        return json({
          id: 'u1',
          permissions:
            options.permissions === undefined
              ? ['manage_account']
              : options.permissions,
        });
      if (path === '/api/v1/weekly-report/preferences') {
        if (init?.method === 'PUT') {
          const body = JSON.parse(String(init.body));
          puts.push(body);
          Object.assign(prefs, body);
        }
        return json(prefs);
      }
      return json({}, 404);
    });
  return { stub, puts };
}

async function mount(): Promise<WeeklyReportSettings> {
  const el = await fixture<WeeklyReportSettings>(
    html`<weekly-report-settings></weekly-report-settings>`
  );
  return el;
}

const switchFor = (el: WeeklyReportSettings, field: string) =>
  el.shadowRoot!.querySelector(`sl-switch[data-field="${field}"]`) as
    (HTMLElement & { checked: boolean; disabled: boolean }) | null;

describe('weekly-report-settings', () => {
  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
  });
  afterEach(() => {
    sinon.restore();
    localStorage.removeItem('accessToken');
    invalidateApiCaches();
  });

  it('shows the three account switches with their defaults', async () => {
    stubApi({});
    const el = await mount();
    await waitUntil(() => switchFor(el, 'account_enabled'));
    expect(switchFor(el, 'account_enabled')!.checked).to.be.true;
    expect(switchFor(el, 'members_enabled')!.checked).to.be.true;
    expect(switchFor(el, 'member_policy_findings_enabled')!.checked).to.be
      .false;
    expect(el.shadowRoot!.textContent).to.contain('costs no');
  });

  it('turns member reports off without touching admins', async () => {
    const { puts } = stubApi({});
    const el = await mount();
    await waitUntil(() => switchFor(el, 'members_enabled'));
    const sw = switchFor(el, 'members_enabled')!;
    sw.checked = false;
    sw.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => puts.length === 1);
    expect(puts[0]).to.deep.equal({ members_enabled: false });
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes(
        'Turn on personal reports for members first.'
      )
    );
    expect(switchFor(el, 'member_policy_findings_enabled')!.disabled).to.be
      .true;
    expect(switchFor(el, 'account_enabled')!.checked).to.be.true;
    expect(switchFor(el, 'account_enabled')!.disabled).to.be.false;
  });

  it('is read-only without manage_account', async () => {
    stubApi({ permissions: ['view_policies'] });
    const el = await mount();
    await waitUntil(() => switchFor(el, 'account_enabled'));
    expect(switchFor(el, 'account_enabled')!.disabled).to.be.true;
    expect(el.shadowRoot!.textContent).to.contain(
      'Owners and admins can change these.'
    );
  });

  it('hides switches an older server does not return', async () => {
    stubApi({ prefs: { account_enabled: true, user_enabled: true } });
    const el = await mount();
    await waitUntil(() => switchFor(el, 'account_enabled'));
    expect(switchFor(el, 'members_enabled')).to.equal(null);
  });

  it('renders nothing without the governance_review capability', async () => {
    const { stub } = stubApi({ capability: false });
    const el = await mount();
    await waitUntil(() => stub.called);
    await new Promise((r) => setTimeout(r, 50));
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });
});
