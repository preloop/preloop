import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import './flow-executions-view';
import { FlowExecutionsView } from './flow-executions-view';
import { resetConfirmDialogForTests } from '../../components/confirm-dialog';
import type { ConfirmDialog } from '../../components/confirm-dialog';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import {
  FINISHED_EXECUTION,
  FINISHED_EXECUTION_COST,
  FINISHED_EXECUTION_TOOL_CALLS,
} from './test-finished-execution';

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const EXECUTIONS = [
  {
    id: 'exec-aaaaaaaa-1',
    flow_id: 'flow-1',
    flow_name: 'Nightly Sync',
    status: 'SUCCEEDED',
    start_time: '2026-03-09T10:00:00Z',
    end_time: '2026-03-09T10:05:00Z',
    tool_calls_count: 3,
  },
  {
    id: 'exec-bbbbbbbb-2',
    flow_id: 'flow-2',
    flow_name: 'Triage',
    status: 'RUNNING',
    start_time: '2026-03-09T11:00:00Z',
  },
];

describe('FlowExecutionsView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    sinon.restore();
    fetchStub = undefined as unknown as sinon.SinonStub;
    resetConfirmDialogForTests();
    document.body.querySelectorAll('sl-alert').forEach((a) => a.remove());
    localStorage.clear();
  });

  function stub(rows: unknown[]) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(
        async () => new Response(JSON.stringify(rows), { status: 200 })
      );
  }

  it('renders the header and empty state with no executions', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Flow executions');
    // The default window is 30 days, so the empty state names it.
    expect(el.shadowRoot?.textContent).to.contain(
      'No executions in the last 30 days'
    );
  });

  it('offers all time from the empty state and drops the window', async () => {
    const requested: string[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      requested.push(String(input));
      return new Response(JSON.stringify([]), { status: 200 });
    });
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    expect(requested.some((url) => url.includes('started_after='))).to.be.true;
    const widen = el.shadowRoot?.querySelector(
      'sl-button.widen-range'
    ) as HTMLElement;
    expect(widen, 'empty state offers all time').to.exist;
    expect((widen.textContent || '').trim()).to.equal('Show all time');

    requested.length = 0;
    widen.click();
    await tick();
    await el.updateComplete;

    expect(requested.length).to.be.greaterThan(0);
    expect(requested.every((url) => !url.includes('started_after='))).to.be
      .true;
    const range = el.shadowRoot?.querySelector(
      'time-range-select'
    ) as HTMLElement & { value: string };
    expect(range.value).to.equal('all');
    expect(el.shadowRoot?.textContent).to.contain('No executions found');
  });

  it('renders a table row per execution', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const rows = el.shadowRoot?.querySelectorAll('tbody tr');
    expect(rows?.length).to.equal(2);
    expect(el.shadowRoot?.textContent).to.contain('Nightly Sync');
    expect(el.shadowRoot?.textContent).to.contain('Triage');
  });

  it('chips the failure category after the status pill, when there is one', async () => {
    fetchStub = stub([
      {
        id: 'exec-cccccccc-3',
        flow_id: 'flow-3',
        flow_name: 'Refunds',
        status: 'FAILED',
        start_time: '2026-03-09T12:00:00Z',
        end_time: '2026-03-09T12:01:00Z',
        failure_category: 'runner_conflict',
      },
      ...EXECUTIONS,
    ]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const cells = Array.from(
      el.shadowRoot!.querySelectorAll('tbody tr .status-cell')
    );
    const badges = Array.from(cells[0].querySelectorAll('sl-badge')).map(
      (badge) => (badge.textContent || '').trim()
    );
    // Red stays in the pill; the category is the soft chip after it.
    expect(badges).to.eql(['Failed', 'Runner conflict']);
    const chip = cells[0].querySelector('sl-badge[data-failure-category]')!;
    expect(chip.getAttribute('variant')).to.equal('neutral');
    expect(chip.closest('sl-tooltip')!.getAttribute('content')).to.contain(
      'a job of the same name'
    );

    // The runs that carry no category look exactly as they did.
    expect(cells[1].querySelectorAll('sl-badge').length).to.equal(1);
  });

  it('fits the table inside its wrapper at 1440', async () => {
    // 1125px is the content width the console gives this table at a 1440
    // viewport, where the content-sized layout measured 1250px and pushed
    // the cost column and the kebab off-screen.
    fetchStub = stub([
      {
        id: 'exec-dddddddd-4',
        flow_id: 'flow-4',
        flow_name: 'A flow with a decidedly long name for one column',
        status: 'SUCCEEDED',
        start_time: '2026-03-09T10:00:00Z',
        end_time: '2026-03-09T10:05:00Z',
        tool_calls_count: 16,
        estimated_cost: 0.08,
        trigger_subject:
          'preloop/preloop #138 · Pull request opened · 949d625b',
        model_alias: 'deepseek/deepseek-v4-pro',
        provider_name: 'deepseek',
        runner: { kind: 'hosted', pool: 'hosted' },
      },
      ...EXECUTIONS,
    ]);
    const host = (await fixture(
      html`<div style="width: 1125px;">
        <flow-executions-view></flow-executions-view>
      </div>`
    )) as HTMLElement;
    const el = host.querySelector('flow-executions-view') as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const wrapper = el.shadowRoot!.querySelector(
      '.table-wrapper'
    ) as HTMLElement;
    const table = wrapper.querySelector('table') as HTMLElement;
    expect(table.scrollWidth).to.be.at.most(wrapper.clientWidth);

    // The column that went off-screen was the last one, so state it: the
    // kebab's own header ends inside the wrapper, not past its right edge.
    const headers = [...table.querySelectorAll('thead th')];
    const actions = headers[headers.length - 1] as HTMLElement;
    const wrapperBox = wrapper.getBoundingClientRect();
    expect(
      actions.getBoundingClientRect().right,
      'the actions column ends inside the wrapper'
    ).to.be.at.most(wrapperBox.left + wrapper.clientWidth + 1);
  });

  it('prints the tool calls and cost the execution page states', async () => {
    // The row is the same fixture the execution page test opens. On staging
    // the two said 0 vs 16 tool calls and $0.03 vs $0.08 for one run, because
    // the table printed the stored rollups and the page showed the
    // aggregation; the server now projects the aggregation onto the row.
    fetchStub = stub([FINISHED_EXECUTION]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const cells = [
      ...el.shadowRoot!.querySelectorAll('tbody tr td.numeric'),
    ].map((cell) => (cell.textContent || '').trim());
    // Tool calls, then tokens (empty here: this fixture carries no gateway
    // token usage), then the cost those tokens bought.
    expect(cells).to.deep.equal([
      String(FINISHED_EXECUTION_TOOL_CALLS),
      '',
      `$${FINISHED_EXECUTION_COST.toFixed(2)}`,
    ]);
  });

  it('preselects the status and flow filters from the query string', async () => {
    const requested: string[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = String(input);
      requested.push(url);
      const body = url.includes('/executions')
        ? EXECUTIONS
        : [{ id: 'flow-1', name: 'Nightly Sync' }];
      return new Response(JSON.stringify(body), { status: 200 });
    });
    const original = window.location.href;
    window.history.replaceState(
      {},
      '',
      '/console/flows/executions?status=FAILED&flow_id=flow-1'
    );

    try {
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;

      expect(requested.some((url) => url.includes('status=FAILED'))).to.be.true;
      expect(requested.some((url) => url.includes('flow_id=flow-1'))).to.be
        .true;
      const status = el.shadowRoot?.querySelector(
        'sl-select.status-filter'
      ) as HTMLElement & { value: string };
      expect(status.value).to.equal('FAILED');
      const flow = el.shadowRoot?.querySelector(
        'sl-select.flow-filter'
      ) as HTMLElement & { value: string };
      expect(flow.value).to.equal('flow-1');
    } finally {
      window.history.replaceState({}, '', original);
    }
  });

  it('accepts the shorter ?flow= deep link and widens the range for it', async () => {
    const requested: string[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      requested.push(String(input));
      return new Response(JSON.stringify(EXECUTIONS), { status: 200 });
    });
    const original = window.location.href;
    window.history.replaceState(
      {},
      '',
      '/console/flows/executions?flow=flow-2'
    );

    try {
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;

      expect(requested.some((url) => url.includes('flow_id=flow-2'))).to.be
        .true;
      // A link to one flow's runs must not hide them behind a 30 day window.
      expect(requested.some((url) => url.includes('started_after='))).to.be
        .false;
      const range = el.shadowRoot?.querySelector(
        'time-range-select'
      ) as HTMLElement & { value: string };
      expect(range.value).to.equal('all');
    } finally {
      window.history.replaceState({}, '', original);
    }
  });

  it('drops a pending search when the view is torn down', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const toolbar = el.shadowRoot?.querySelector('list-toolbar');
    toolbar?.dispatchEvent(
      new CustomEvent('search-change', { detail: { value: 'nightly' } })
    );
    const callsBefore = fetchStub.callCount;
    el.remove();
    await tick(400);

    // The debounced request must not fire against a detached element.
    expect(fetchStub.callCount).to.equal(callsBefore);
  });

  it('offers every execution status in the status filter', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const options = Array.from(
      el.shadowRoot?.querySelectorAll('sl-select.status-filter sl-option') || []
    ).map((option) => option.getAttribute('value'));
    expect(options).to.eql([
      '',
      'RUNNING',
      'PENDING',
      'SUCCEEDED',
      'FAILED',
      'CANCELLED',
    ]);
  });

  it('names the filter selects for a screen reader without printing them', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const flow = el.shadowRoot?.querySelector('sl-select.flow-filter');
    const status = el.shadowRoot?.querySelector('sl-select.status-filter');
    expect(flow?.getAttribute('label')).to.equal('Flow');
    expect(status?.getAttribute('label')).to.equal('Status');
    const printed = flow?.shadowRoot?.querySelector(
      '[part~="form-control-label"]'
    ) as HTMLElement | null;
    // Named, but clipped: the bar has no room to print the label.
    expect(printed && printed.getBoundingClientRect().height).to.be.lessThan(2);
  });

  it('reloads executions when the status filter changes', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    const callsBefore = fetchStub.callCount;

    const select = el.shadowRoot?.querySelector(
      'sl-select.status-filter'
    ) as HTMLElement & { value: string };
    expect(select).to.exist;
    select.value = 'RUNNING';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await tick();
    await el.updateComplete;

    expect(fetchStub.callCount).to.be.greaterThan(callsBefore);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('status=RUNNING'))
    ).to.be.true;
  });

  it('counts the page against the matched total the server reports', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(JSON.stringify(EXECUTIONS), {
          status: 200,
          headers: { 'X-Total-Count': '1412' },
        })
    );
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const count = el.shadowRoot?.querySelector('[slot="count"]');
    expect((count?.textContent || '').trim()).to.equal('2 of 1,412 executions');
  });

  it('counts only what it can see when the server sends no total', async () => {
    fetchStub = stub(EXECUTIONS);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;

    const count = el.shadowRoot?.querySelector('[slot="count"]');
    expect((count?.textContent || '').trim()).to.equal('2 executions');
  });

  it('surfaces the flow-executions endpoint with pagination params', async () => {
    fetchStub = stub([]);
    const el = (await fixture(
      html`<flow-executions-view></flow-executions-view>`
    )) as FlowExecutionsView;
    await tick();
    await el.updateComplete;
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/api/v1/flows/executions'))
    ).to.be.true;
  });

  describe('duration column', () => {
    async function renderRows(rows: unknown[]) {
      fetchStub = stub(rows);
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;
      return el;
    }

    const cellText = (el: FlowExecutionsView, rowIndex: number) => {
      const cells = el.shadowRoot
        ?.querySelectorAll('tbody tr')
        [rowIndex]?.querySelectorAll('td');
      // Flow, Subject, Status, Started, Duration, Model, Tool calls, $ est.
      return (cells?.[4]?.textContent || '').replace(/\s+/g, ' ').trim();
    };

    it('names the time columns Started and Duration', async () => {
      const el = await renderRows(EXECUTIONS);
      const headers = Array.from(
        el.shadowRoot?.querySelectorAll('thead th') || []
      ).map((th) => (th.textContent || '').trim());

      expect(headers).to.contain('Duration');
      expect(headers).to.contain('Started');
      expect(headers).to.not.contain('End Time');
      expect(headers).to.not.contain('Start Time');
    });

    it('shows the completed duration for a finished execution', async () => {
      const el = await renderRows([
        {
          id: 'exec-done',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:04:32Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('4m 32s');
    });

    it('shows live elapsed time for a running execution', async () => {
      const el = await renderRows([
        {
          id: 'exec-running',
          flow_id: 'flow-2',
          flow_name: 'Triage',
          status: 'RUNNING',
          start_time: new Date(Date.now() - 65_000).toISOString(),
        },
      ]);

      const text = cellText(el, 0);
      expect(text).to.match(/^Running · \d+m \d+s$/);
    });

    it('shows an em dash for a terminal execution with no end time', async () => {
      const el = await renderRows([
        {
          id: 'exec-legacy',
          flow_id: 'flow-3',
          flow_name: 'Legacy',
          status: 'FAILED',
          start_time: '2026-03-09T10:00:00Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('—');
    });

    it('shows an em dash when the timestamps are unusable', async () => {
      const el = await renderRows([
        {
          id: 'exec-skew',
          flow_id: 'flow-4',
          flow_name: 'Skewed',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:05:00Z',
          end_time: '2026-03-09T10:00:00Z',
        },
      ]);

      expect(cellText(el, 0)).to.equal('—');
    });
  });

  describe('wave 7 columns', () => {
    async function renderRows(rows: unknown[]) {
      fetchStub = stub(rows);
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;
      return el;
    }

    it('lists the wave 7 columns in order', async () => {
      const el = await renderRows(EXECUTIONS);
      const headers = Array.from(
        el.shadowRoot?.querySelectorAll('thead th') || []
      ).map((th) => (th.textContent || '').trim());

      expect(headers).to.eql([
        'Flow',
        'Subject',
        'Status',
        'Started',
        'Duration',
        'Model',
        'Tool calls',
        // Tokens before cost.
        'Tokens',
        '$ est.',
        '',
      ]);
    });

    it('shows the alias alone and +N for a multi model run', async () => {
      const el = await renderRows([
        {
          id: 'exec-multi',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          model_alias: 'anthropic/claude-sonnet-4',
          provider_name: 'anthropic',
          models_used: [
            {
              model_alias: 'anthropic/claude-sonnet-4',
              provider_name: 'anthropic',
              request_count: 4,
            },
            {
              model_alias: 'openai/gpt-5',
              provider_name: 'openai',
              request_count: 1,
            },
          ],
        },
      ]);

      const cell = el.shadowRoot?.querySelector('tbody .model-cell');
      // The list has no provider column, so the cell prints the alias once:
      // "anthropic/claude-sonnet-4 anthropic" said the vendor twice.
      expect(
        cell?.querySelector('.execution-model-alias')?.textContent
      ).to.equal('claude-sonnet-4');
      expect(cell?.querySelector('.execution-model-provider')).to.not.exist;
      expect(
        cell?.querySelector('.execution-model')?.getAttribute('title')
      ).to.contain('anthropic');
      expect(
        cell?.querySelector('.execution-model-more')?.textContent
      ).to.equal('+1');
      expect(
        cell?.querySelector('.execution-model')?.getAttribute('title')
      ).to.contain('openai/gpt-5');
    });

    it('chips only the runner that is not the account default', async () => {
      const el = await renderRows([
        {
          id: 'exec-hosted',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          runner: { kind: 'hosted', name: 'Preloop hosted' },
        },
        {
          id: 'exec-private',
          flow_id: 'flow-2',
          flow_name: 'Triage',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          runner: { kind: 'private', name: 'Office Mac' },
        },
      ]);

      // The account runs hosted by default, so "Hosted" on every row would
      // just repeat the default; only the run that left it is chipped.
      const badges = Array.from(
        el.shadowRoot!.querySelectorAll('[data-testid="runner-kind-badge"]')
      );
      expect(badges.map((badge) => badge.textContent!.trim())).to.eql([
        'Private',
      ]);
      expect(badges[0]!.getAttribute('data-runner-kind')).to.equal('private');
    });

    it('shows a dash when a run has no attributable model', async () => {
      const el = await renderRows([
        {
          id: 'exec-no-model',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
        },
      ]);

      const cell = el.shadowRoot?.querySelector('tbody .model-cell');
      expect((cell?.textContent || '').trim()).to.equal('\u2014');
      expect(cell?.querySelector('.execution-model-more')).to.not.exist;
    });

    it('writes the status as a soft title case chip', async () => {
      const el = await renderRows(EXECUTIONS);
      const chips = Array.from(
        el.shadowRoot?.querySelectorAll(
          'tbody .status-cell sl-badge:not([data-testid="runner-kind-badge"])'
        ) || []
      );

      expect(chips[0]?.textContent?.trim()).to.equal('Succeeded');
      expect(chips[0]?.getAttribute('variant')).to.equal('success');
      expect(chips[0]?.classList.contains('solid')).to.be.false;
      expect(chips[1]?.textContent?.trim()).to.equal('Running');
    });

    it('shows Started as relative time with the absolute time in the title', async () => {
      const el = await renderRows(EXECUTIONS);
      const started = el.shadowRoot?.querySelector('tbody .started-cell');

      expect(started?.getAttribute('title')).to.contain('2026-03-09');
      expect((started?.textContent || '').trim()).to.not.contain('2026-03-09');
    });

    it('states tokens before cost, split in and out', async () => {
      const el = await renderRows([
        {
          id: 'exec-tokens',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          estimated_cost: 0.083,
          token_usage: {
            prompt_tokens: 12400,
            completion_tokens: 3100,
            total_tokens: 15500,
            input_tokens: 12400,
            output_tokens: 3100,
            cache_read_tokens: 8200,
            cache_write_tokens: 0,
            uncached_input_tokens: 3900,
            cache_hit_ratio: 0.6777,
          },
        },
      ]);

      const cells = Array.from(
        el.shadowRoot?.querySelectorAll('tbody tr td') || []
      );
      const tokenIndex = cells.findIndex((cell) =>
        cell.querySelector('token-figures')
      );
      const costIndex = cells.findIndex((cell) =>
        (cell.textContent || '').includes('$0.08')
      );
      expect(tokenIndex).to.be.greaterThan(-1);
      expect(tokenIndex, 'tokens before cost').to.be.lessThan(costIndex);

      const figures = cells[tokenIndex].querySelector('token-figures')!;
      await (figures as unknown as { updateComplete: Promise<unknown> })
        .updateComplete;
      const text = (figures.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain('12.4K in');
      expect(text).to.contain('3.1K out');
      expect(text).to.contain('cache 68% hit');
    });

    it('shows the estimated cost, dashing an unpriced run', async () => {
      const el = await renderRows([
        {
          id: 'exec-cost',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
          estimated_cost: 0.083,
        },
        {
          id: 'exec-unpriced',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T09:00:00Z',
          end_time: '2026-03-09T09:01:00Z',
          estimated_cost: 0,
        },
      ]);

      const costCells = Array.from(
        el.shadowRoot?.querySelectorAll('tbody tr') || []
      ).map((row) => (row.querySelectorAll('td')[8]?.textContent || '').trim());
      expect(costCells).to.eql(['$0.08', '\u2014']);
    });

    it('anchors the flow name at the execution and makes the row clickable', async () => {
      const el = await renderRows(EXECUTIONS);
      const row = el.shadowRoot?.querySelector('tbody tr');
      const link = row?.querySelector('a.row-link') as HTMLAnchorElement;

      expect(link?.getAttribute('href')).to.contain(
        '/console/flows/executions/exec-aaaaaaaa-1'
      );
      expect(row?.classList.contains('execution-row')).to.be.true;
    });

    it('offers open, open session and the run control in the row menu', async () => {
      const el = await renderRows(EXECUTIONS);
      const menus = Array.from(
        el.shadowRoot?.querySelectorAll('tbody resource-actions') || []
      ) as Array<
        HTMLElement & { actions: Array<{ id: string; href?: string }> }
      >;

      expect(menus.length).to.equal(2);
      const finished = menus[0].actions.map((action) => action.id);
      expect(finished).to.eql(['open', 'open-session']);
      expect(menus[0].actions[1].href).to.contain('?tab=transcript');
      const running = menus[1].actions.map((action) => action.id);
      expect(running).to.contain('cancel');
    });

    it('offers retry on a failed run', async () => {
      const el = await renderRows([
        {
          id: 'exec-failed',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'FAILED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:01:00Z',
        },
      ]);
      const menu = el.shadowRoot?.querySelector(
        'tbody resource-actions'
      ) as HTMLElement & { actions: Array<{ id: string }> };

      expect(menu.actions.map((action) => action.id)).to.contain('retry');
    });

    it('asks for 25 rows a page', async () => {
      await renderRows([]);
      expect(
        fetchStub.getCalls().some((c) => String(c.args[0]).includes('limit=26'))
      ).to.be.true;
    });

    it('says live updates are off before a connection exists, never disconnected', async () => {
      const el = await renderRows(EXECUTIONS);
      const status = el.shadowRoot?.querySelector('.connection-status');

      expect(status?.getAttribute('data-connection')).to.equal('off');
      expect((status?.textContent || '').trim()).to.equal('Live updates off');
      expect(el.shadowRoot?.textContent).to.not.contain('Disconnected');
      expect(el.shadowRoot?.querySelector('.connection-dot.dropped')).to.not
        .exist;
    });

    it('turns the indicator red only after a live connection dropped', async () => {
      const el = await renderRows(EXECUTIONS);
      (el as unknown as { wsConnected: boolean }).wsConnected = true;
      (el as unknown as { wsWasConnected: boolean }).wsWasConnected = true;
      await el.updateComplete;
      expect(
        el.shadowRoot?.querySelector('.connection-status')?.textContent?.trim()
      ).to.equal('Live updates on');

      (el as unknown as { wsConnected: boolean }).wsConnected = false;
      await el.updateComplete;
      const status = el.shadowRoot?.querySelector('.connection-status');
      expect(status?.getAttribute('data-connection')).to.equal('dropped');
      expect((status?.textContent || '').trim()).to.equal('Live updates lost');
    });
  });

  describe('wave 7 review fixes', () => {
    async function render() {
      const el = (await fixture(
        html`<flow-executions-view></flow-executions-view>`
      )) as FlowExecutionsView;
      await tick();
      await el.updateComplete;
      return el;
    }

    async function clickDialogButton(label: string) {
      const dialog = document.body.querySelector(
        'confirm-dialog'
      ) as ConfirmDialog | null;
      expect(dialog, 'confirm-dialog is mounted').to.exist;
      await dialog!.updateComplete;
      const button = [
        ...(dialog!.shadowRoot?.querySelectorAll('sl-button') || []),
      ].find((candidate) => candidate.textContent?.trim() === label);
      expect(button, `dialog button "${label}"`).to.exist;
      (button as HTMLElement).click();
      await tick(20);
    }

    function actionById(el: FlowExecutionsView, id: string) {
      const menus = Array.from(
        el.shadowRoot?.querySelectorAll('tbody resource-actions') || []
      ) as Array<
        HTMLElement & { actions: Array<{ id: string; onClick?: () => void }> }
      >;
      for (const menu of menus) {
        const action = menu.actions.find((candidate) => candidate.id === id);
        if (action) return action;
      }
      throw new Error(`no "${id}" action in the row menus`);
    }

    it('states a failed load, keeps live updates, and retries on demand', async () => {
      let fail = true;
      fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
        if (fail) throw new Error('Network unreachable');
        return new Response(JSON.stringify(EXECUTIONS), { status: 200 });
      });
      const connect = sinon.spy(unifiedWebSocketManager, 'subscribe');

      const el = await render();

      expect(el.shadowRoot?.querySelector('sl-alert.load-error')).to.exist;
      expect(el.shadowRoot?.textContent).to.contain(
        'Could not load the executions'
      );
      // The old code threw out of connectedCallback before subscribing.
      expect(connect.called, 'still subscribed to live updates').to.be.true;
      expect(el.shadowRoot?.textContent).to.not.contain('No executions found');

      fail = false;
      (
        el.shadowRoot?.querySelector('.load-error .retry-button') as HTMLElement
      ).click();
      await tick();
      await el.updateComplete;

      expect(el.shadowRoot?.querySelector('sl-alert.load-error')).to.not.exist;
      expect(el.shadowRoot?.querySelectorAll('tbody tr').length).to.equal(2);
    });

    it('asks before it cancels a run and does nothing when the answer is no', async () => {
      const requests: string[] = [];
      fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
        requests.push(String(input));
        return new Response(JSON.stringify(EXECUTIONS), { status: 200 });
      });
      const el = await render();

      actionById(el, 'cancel').onClick?.();
      await tick(20);

      const dialog = document.body.querySelector(
        'confirm-dialog'
      ) as ConfirmDialog;
      await dialog.updateComplete;
      expect(dialog.shadowRoot?.textContent).to.contain('Stop the run of');
      expect(
        dialog.shadowRoot?.querySelector('sl-dialog')?.getAttribute('label')
      ).to.equal('Cancel run');

      await clickDialogButton('Keep running');
      expect(requests.some((url) => url.includes('/command'))).to.be.false;
    });

    it('sends the stop only after the run cancel is confirmed', async () => {
      const requests: string[] = [];
      fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
        requests.push(String(input));
        return new Response(JSON.stringify(EXECUTIONS), { status: 200 });
      });
      const el = await render();

      actionById(el, 'cancel').onClick?.();
      await tick(20);
      await clickDialogButton('Cancel run');

      expect(
        requests.some((url) =>
          url.includes('/api/v1/flows/executions/exec-bbbbbbbb-2/command')
        )
      ).to.be.true;
    });

    it('says so when a retry comes back without a run to open', async () => {
      fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
        if (String(input).includes('/retry')) {
          return new Response(JSON.stringify({}), { status: 200 });
        }
        return new Response(
          JSON.stringify([
            {
              id: 'exec-failed',
              flow_id: 'flow-1',
              flow_name: 'Nightly Sync',
              status: 'FAILED',
              start_time: '2026-03-09T10:00:00Z',
              end_time: '2026-03-09T10:01:00Z',
            },
          ]),
          { status: 200 }
        );
      });
      const el = await render();
      const toasts: string[] = [];
      el.addEventListener('show-toast', (event) => {
        toasts.push((event as CustomEvent).detail.message);
      });

      actionById(el, 'retry').onClick?.();
      await tick();

      expect(toasts).to.eql([
        'The retry did not return a new run. Check the executions list.',
      ]);
    });

    it('drops the connection-state listener when the view goes away', async () => {
      fetchStub = stub(EXECUTIONS);
      const unsubscribeState = sinon.spy();
      sinon
        .stub(unifiedWebSocketManager, 'onStateChange')
        .returns(unsubscribeState);

      const el = await render();
      expect(unsubscribeState.called).to.be.false;

      el.remove();
      await tick(0);

      expect(unsubscribeState.calledOnce, 'state listener released').to.be.true;
    });
  });
});
