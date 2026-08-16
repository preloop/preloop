import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './schedule-config-editor.ts';
import type {
  ScheduleConfigEditor,
  ScheduleConfig,
} from './schedule-config-editor';
import { browserTimezone } from './schedule-config-editor';
import { invalidateApiCaches } from '../api';

describe('ScheduleConfigEditor', () => {
  let fetchStub: sinon.SinonStub;

  function stubPreview(
    handler: (config: any) => { status: number; body: unknown }
  ) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.includes('/api/v1/flows/schedule/preview')) {
          const payload = JSON.parse(String(init?.body || '{}'));
          const { status, body } = handler(payload.schedule_config);
          return new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({ detail: `Unhandled: ${url}` }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        });
      });
  }

  function okPreview(config: any) {
    return {
      status: 200,
      body: {
        type: config.type,
        description: `Preview of ${config.type}`,
        timezone: config.timezone,
        next_run_times: [
          '2026-08-17T09:00:00+00:00',
          '2026-08-18T09:00:00+00:00',
          '2026-08-19T09:00:00+00:00',
        ],
      },
    };
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  it('defaults to a daily schedule in the browser timezone', async () => {
    fetchStub = stubPreview(okPreview);
    const element = (await fixture(
      html`<schedule-config-editor></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    expect(element.value).to.deep.equal({
      type: 'daily',
      at: '09:00',
      timezone: browserTimezone(),
    });
  });

  it('shows the next run times from the backend preview endpoint', async () => {
    fetchStub = stubPreview(okPreview);
    const element = (await fixture(
      html`<schedule-config-editor></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    await waitUntil(
      () =>
        element.shadowRoot?.querySelector('[data-testid="schedule-preview"]'),
      'preview never rendered',
      { timeout: 4000 }
    );

    const preview = element.shadowRoot?.querySelector(
      '[data-testid="schedule-preview"]'
    );
    expect(preview?.textContent).to.contain('Preview of daily');
    const runs = preview?.querySelectorAll('li');
    expect(runs?.length).to.equal(3);
  });

  it('emits an interval config when switching mode', async () => {
    fetchStub = stubPreview(okPreview);
    const changes: ScheduleConfig[] = [];
    const element = (await fixture(
      html`<schedule-config-editor
        @schedule-change=${(e: CustomEvent) => changes.push(e.detail.value)}
      ></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    const radioGroup = element.shadowRoot?.querySelector('sl-radio-group');
    expect(radioGroup).to.exist;
    (radioGroup as any).value = 'interval';
    radioGroup?.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    const latest = changes[changes.length - 1] as any;
    expect(latest.type).to.equal('interval');
    expect(latest.every).to.equal(1);
    expect(latest.unit).to.equal('hours');
    expect(latest.timezone).to.equal(browserTimezone());
  });

  it('switches to cron behind the Advanced toggle', async () => {
    fetchStub = stubPreview(okPreview);
    const changes: ScheduleConfig[] = [];
    const element = (await fixture(
      html`<schedule-config-editor
        @schedule-change=${(e: CustomEvent) => changes.push(e.detail.value)}
      ></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    const toggle = element.shadowRoot?.querySelector('sl-switch');
    expect(toggle).to.exist;
    (toggle as any).checked = true;
    toggle?.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    const latest = changes[changes.length - 1] as any;
    expect(latest.type).to.equal('cron');
    expect(latest.expr).to.equal('0 9 * * *');
    // Friendly mode radios are hidden in cron mode
    expect(element.shadowRoot?.querySelector('sl-radio-group')).to.not.exist;
  });

  it('surfaces backend min-interval validation errors inline', async () => {
    fetchStub = stubPreview(() => ({
      status: 422,
      body: {
        detail: [
          {
            loc: ['body', 'schedule_config', 'interval'],
            msg: 'Value error, Interval of 1 minutes is below the minimum interval of 5 minutes',
            type: 'value_error',
          },
        ],
      },
    }));
    const element = (await fixture(
      html`<schedule-config-editor
        .value=${{
          type: 'interval',
          every: 1,
          unit: 'minutes',
          timezone: 'UTC',
        }}
      ></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    await waitUntil(
      () =>
        element.shadowRoot?.querySelector(
          '[data-testid="schedule-preview-error"]'
        ),
      'error never rendered',
      { timeout: 4000 }
    );

    expect(
      element.shadowRoot?.querySelector(
        '[data-testid="schedule-preview-error"]'
      )?.textContent
    ).to.contain('below the minimum interval of 5 minutes');
  });

  it('rejects a weekly schedule without days client-side', async () => {
    fetchStub = stubPreview(okPreview);
    const element = (await fixture(
      html`<schedule-config-editor
        .value=${{ type: 'weekly', days: [], at: '09:00', timezone: 'UTC' }}
      ></schedule-config-editor>`
    )) as ScheduleConfigEditor;

    await waitUntil(
      () =>
        element.shadowRoot?.querySelector(
          '[data-testid="schedule-preview-error"]'
        ),
      'error never rendered',
      { timeout: 4000 }
    );

    expect(
      element.shadowRoot?.querySelector(
        '[data-testid="schedule-preview-error"]'
      )?.textContent
    ).to.contain('Select at least one day');
    const previewCalls = fetchStub
      .getCalls()
      .filter((call) =>
        String(call.args[0]).includes('/flows/schedule/preview')
      );
    expect(previewCalls.length).to.equal(0);
  });
});
