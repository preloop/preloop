import { expect, fixture, html, oneEvent, waitUntil } from '@open-wc/testing';
import { mockApi, type MockApi } from '../test-helpers/capability-api';
import { sampleFleetMap } from '../test-helpers/fleet-map-graph';
import './inventory-card.ts';
import { INVENTORY_TAB_STORAGE_KEY } from './inventory-card';
import {
  clearTablePreferences,
  readTablePreferences,
} from '../table/table-preferences';
import type {
  InventoryAgentRow,
  InventoryCard,
  InventoryDeviceRow,
  InventoryFlowRow,
  InventoryModelRow,
  InventoryToolRow,
  InventoryUserRow,
} from './inventory-card';

function agentRow(overrides: Partial<InventoryAgentRow> = {}) {
  return {
    id: 'agent-1',
    name: 'Hermes',
    kind: 'hermes',
    status: { label: 'Active now', variant: 'success' as const },
    modelAlias: 'claude-sonnet-4',
    requests: 1200,
    tokens: 480000,
    cost: 12.5,
    lastSeenAt: new Date(Date.now() - 60000).toISOString(),
    ...overrides,
  };
}

function flowRow(overrides: Partial<InventoryFlowRow> = {}) {
  return {
    id: 'flow-1',
    name: 'Pull Request Reviewer',
    lastRun: {
      id: 'exec-1',
      status: 'SUCCEEDED',
      start_time: new Date(Date.now() - 300000).toISOString(),
      end_time: new Date(Date.now() - 113000).toISOString(),
      trigger_subject: 'preloop/preloop #352',
      trigger_subject_url: 'https://github.com/preloop/preloop/pull/352',
    },
    runs: 12,
    failed: 1,
    cost: 3.4,
    ...overrides,
  };
}

function modelRow(overrides: Partial<InventoryModelRow> = {}) {
  return {
    id: 'model-1',
    alias: 'claude-sonnet-4',
    provider: 'anthropic',
    requests: 900,
    tokens: 320000,
    cost: 8.25,
    ...overrides,
  };
}

function toolRow(overrides: Partial<InventoryToolRow> = {}) {
  return {
    name: 'Bash',
    server: 'claude-code',
    calls: 340,
    failed: 5,
    ...overrides,
  };
}

function userRow(overrides: Partial<InventoryUserRow> = {}) {
  return {
    id: 'user-1',
    name: 'Ada Lovelace',
    role: 'Admin',
    lastLoginAt: new Date(Date.now() - 7200000).toISOString(),
    agentsOwned: 3,
    tokens: 240000,
    cost: 9.5,
    ...overrides,
  };
}

function deviceRow(overrides: Partial<InventoryDeviceRow> = {}) {
  return {
    key: 'fp:aaaa',
    name: 'build-01',
    os: 'Linux',
    href: '/console/settings/device-fleet?device=fp%3Aaaaa',
    people: [
      {
        id: 'user-1',
        label: 'Jane Doe',
        href: '/console/settings/users/user-1',
      },
      { id: null, label: 'jdoe@example.com', href: null },
    ],
    runnerState: 'online',
    runnerLabel: 'Runner online',
    runnerLive: true,
    agentsGoverned: 2,
    agentsUngoverned: 1,
    lastSeenAt: new Date(Date.now() - 120000).toISOString(),
    ...overrides,
  };
}

async function card(
  props: Partial<{
    deviceRows: InventoryDeviceRow[];
    devicesTotal: number;
    showDevices: boolean;
    loadingDevices: boolean | null;
    unavailableTabs: string[];
    agentRows: InventoryAgentRow[];
    flowRows: InventoryFlowRow[];
    modelRows: InventoryModelRow[];
    toolRows: InventoryToolRow[];
    userRows: InventoryUserRow[];
    usersTotal: number;
    showUsers: boolean;
    loading: boolean;
    usageLoading: boolean;
    usageLoadingFlows: boolean;
    usageLoadingFlowCost: boolean;
    usageLoadingModels: boolean;
    usageLoadingTools: boolean;
    flowRunsCapped: boolean;
    timeRange: string;
  }> = {}
): Promise<InventoryCard> {
  const el = await fixture<InventoryCard>(html`
    <inventory-card
      .flowRunsCapped=${props.flowRunsCapped ?? false}
      .agentRows=${props.agentRows ?? [agentRow()]}
      .flowRows=${props.flowRows ?? [flowRow()]}
      .modelRows=${props.modelRows ?? [modelRow()]}
      .toolRows=${props.toolRows ?? [toolRow()]}
      .userRows=${props.userRows ?? []}
      .usersTotal=${props.usersTotal ?? 0}
      ?showUsers=${props.showUsers ?? false}
      .deviceRows=${props.deviceRows ?? []}
      .devicesTotal=${props.devicesTotal ?? 0}
      ?showDevices=${props.showDevices ?? false}
      .loadingDevices=${props.loadingDevices ?? null}
      .unavailableTabs=${props.unavailableTabs ?? []}
      .agentsTotal=${10}
      .flowsTotal=${30}
      .modelsTotal=${16}
      .toolsTotal=${16}
      .loading=${props.loading ?? false}
      .usageLoading=${props.usageLoading ?? false}
      .usageLoadingFlows=${props.usageLoadingFlows ?? null}
      .usageLoadingFlowCost=${props.usageLoadingFlowCost ?? null}
      .usageLoadingModels=${props.usageLoadingModels ?? null}
      .usageLoadingTools=${props.usageLoadingTools ?? null}
      rangeLabel="30d"
      timeRange=${props.timeRange ?? 'this-month'}
    ></inventory-card>
  `);
  await el.updateComplete;
  return el;
}

function tabText(el: InventoryCard): string[] {
  return Array.from(el.shadowRoot!.querySelectorAll('sl-tab')).map((tab) =>
    (tab.textContent || '').replace(/\s+/g, ' ').trim()
  );
}

async function showTab(el: InventoryCard, name: string) {
  const tab = el.shadowRoot!.querySelector(`sl-tab[panel="${name}"]`)!;
  tab.dispatchEvent(
    new CustomEvent('sl-tab-show', {
      detail: { name },
      bubbles: true,
      composed: true,
    })
  );
  await el.updateComplete;
}

describe('inventory-card', () => {
  beforeEach(() => {
    localStorage.removeItem(INVENTORY_TAB_STORAGE_KEY);
  });

  it('leads with a token total, before the money column', async () => {
    const el = await card({
      agentRows: [
        agentRow({
          tokens: 15500,
          tokenUsage: {
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
        }),
      ],
    });

    const headers = Array.from(el.shadowRoot!.querySelectorAll('table th')).map(
      (th) => (th.textContent || '').trim()
    );
    expect(headers.indexOf('Tokens')).to.be.greaterThan(-1);
    expect(headers.indexOf('Tokens')).to.be.lessThan(headers.indexOf('$ est.'));

    const figures = el.shadowRoot!.querySelector(
      'td[data-label="Tokens"] token-figures'
    ) as HTMLElement & { updateComplete: Promise<unknown> };
    expect(figures).to.exist;
    await figures.updateComplete;
    // One compact total: an 11% column cut the split mid-figure (#1647).
    // The split and the cache rate are one hover away.
    expect(figures.hasAttribute('total-only')).to.be.true;
    const shown = figures.shadowRoot?.querySelector('.figures') as HTMLElement;
    expect((shown.textContent || '').trim()).to.equal('15.5K');
    const title = shown.getAttribute('title') || '';
    expect(title).to.contain('12,400 input tokens');
    expect(title).to.contain('3,100 output tokens');
    expect(title).to.contain('68% hit');
  });

  it('fits a nine-figure token total in the Agents column (#1647)', async () => {
    const el = await card({
      agentRows: [
        agentRow({
          tokens: 736_900_000,
          tokenUsage: {
            prompt_tokens: 734_600_000,
            completion_tokens: 2_310_000,
            total_tokens: 736_900_000,
            input_tokens: 734_600_000,
            output_tokens: 2_310_000,
            cache_read_tokens: 600_000_000,
            cache_write_tokens: 0,
            uncached_input_tokens: 134_600_000,
            cache_hit_ratio: 0.8168,
          },
        }),
      ],
    });
    // The Overview's main column at 1440x900.
    el.style.display = 'block';
    el.style.width = '735px';
    await el.updateComplete;
    const cell = el.shadowRoot!.querySelector(
      'td[data-label="Tokens"]'
    ) as HTMLElement;
    const figures = cell.querySelector('token-figures') as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await figures.updateComplete;
    expect(cell.scrollWidth).to.be.at.most(cell.clientWidth + 1);
  });

  it('leads the Flows tab with tokens too, before its money column', async () => {
    // The Flows tab was the one tab of this card still reading
    // "Runs, Failed, $ est." with no volume beside the money.
    const el = await card({
      flowRows: [
        flowRow({
          tokenUsage: {
            prompt_tokens: 9000,
            completion_tokens: 1200,
            total_tokens: 10200,
            input_tokens: 9000,
            output_tokens: 1200,
            cache_read_tokens: 6000,
            cache_write_tokens: 0,
            uncached_input_tokens: 3000,
            cache_hit_ratio: 0.6667,
          },
        }),
      ],
    });
    await showTab(el, 'flows');

    const headers = Array.from(el.shadowRoot!.querySelectorAll('table th')).map(
      (th) => (th.textContent || '').trim()
    );
    expect(headers).to.eql([
      'Flow',
      'Last run',
      'Runs',
      'Failed',
      'Tokens',
      '$ est.',
    ]);

    const figures = el.shadowRoot!.querySelector(
      'td[data-label="Tokens"] token-figures'
    ) as HTMLElement & { updateComplete: Promise<unknown> };
    expect(figures).to.exist;
    await figures.updateComplete;
    const shown = figures.shadowRoot?.querySelector('.figures') as HTMLElement;
    expect((shown.textContent || '').trim()).to.equal('10.2K');
    const title = shown.getAttribute('title') || '';
    expect(title).to.contain('9,000 input tokens');
    expect(title).to.contain('1,200 output tokens');
    expect(title).to.contain('67% hit');
  });

  it('states no flow tokens rather than a zero when none were reported', async () => {
    const el = await card({ flowRows: [flowRow()] });
    await showTab(el, 'flows');
    const figures = el.shadowRoot!.querySelector(
      'td[data-label="Tokens"] token-figures'
    ) as HTMLElement & { updateComplete: Promise<unknown> };
    await figures.updateComplete;
    expect((figures.shadowRoot?.textContent || '').trim()).to.equal('-');
  });

  it('carries the counts in the tab labels', async () => {
    const el = await card();
    expect(tabText(el)).to.eql([
      'Agents 10',
      'Flows 30',
      'Models 16',
      'Tools 16',
    ]);
  });

  it('holds only what the account has, never approvals or sessions', async () => {
    // An approval that has been decided and a session that has ended are
    // records of something that happened, not things the account holds.
    // Pending approvals live in the attention strip; the record of both
    // lives on the Overview's Audit trail line.
    const el = await card({ showUsers: true });
    const labels = tabText(el).map((label) => label.toLowerCase());
    expect(labels.some((label) => label.includes('approval'))).to.be.false;
    expect(labels.some((label) => label.includes('session'))).to.be.false;
    expect(el.shadowRoot?.textContent).to.not.contain('Approvals');
    expect(el.shadowRoot?.textContent).to.not.contain('Sessions');
  });

  it('opens on Agents and remembers the tab you left it on', async () => {
    const el = await card();
    expect(el.shadowRoot?.querySelector('a[href="/console/agents/agent-1"]')).to
      .exist;

    await showTab(el, 'models');
    expect(localStorage.getItem(INVENTORY_TAB_STORAGE_KEY)).to.equal('models');

    const reopened = await card();
    expect(
      reopened.shadowRoot?.querySelector('a[href="/console/ai-models/model-1"]')
    ).to.exist;
  });

  it('offers the sorts the tab has, defaulting to the first', async () => {
    const el = await card();
    const select = () =>
      el.shadowRoot!.querySelector('sl-select') as HTMLElement & {
        value: string;
      };
    const options = () =>
      Array.from(el.shadowRoot!.querySelectorAll('sl-option')).map((option) =>
        (option.textContent || '').trim()
      );

    expect(options()).to.eql(['Last active', 'Requests', 'Spend']);
    expect(select().value).to.equal('last-active');

    await showTab(el, 'flows');
    expect(options()).to.eql(['Last run', 'Runs', 'Spend', 'Failures']);
    expect(select().value).to.equal('last-run');

    await showTab(el, 'models');
    expect(options()).to.eql(['Spend', 'Requests']);
    expect(select().value).to.equal('spend');

    await showTab(el, 'tools');
    expect(options()).to.eql(['Calls', 'Failures']);
    expect(select().value).to.equal('calls');
  });

  it('sorts agents by last active by default and by spend on request', async () => {
    const el = await card({
      agentRows: [
        agentRow({
          id: 'quiet',
          name: 'Quiet',
          cost: 90,
          lastSeenAt: new Date(Date.now() - 86400000).toISOString(),
        }),
        agentRow({ id: 'busy', name: 'Busy', cost: 1 }),
      ],
    });

    const names = () =>
      Array.from(el.shadowRoot!.querySelectorAll('a.row-name')).map((a) =>
        (a.textContent || '').trim()
      );
    expect(names()).to.eql(['Busy', 'Quiet']);

    const select = el.shadowRoot!.querySelector('sl-select') as HTMLElement & {
      value: string;
    };
    select.value = 'spend';
    select.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await el.updateComplete;
    expect(names()).to.eql(['Quiet', 'Busy']);
  });

  it('links each row to its own page, and flow runs to the filtered list', async () => {
    const el = await card();
    expect(el.shadowRoot?.querySelector('a[href="/console/agents/agent-1"]')).to
      .exist;

    await showTab(el, 'flows');
    expect(el.shadowRoot?.querySelector('a[href="/console/flows/flow-1"]')).to
      .exist;
    expect(
      el.shadowRoot?.querySelector(
        'a[href="/console/flows/executions?flow_id=flow-1"]'
      )
    ).to.exist;
    // The subject leaves the console for the pull request it names.
    expect(
      el.shadowRoot?.querySelector(
        'a[href="https://github.com/preloop/preloop/pull/352"]'
      )
    ).to.exist;

    await showTab(el, 'tools');
    expect(el.shadowRoot?.querySelector('a[href="/console/tools#tool=Bash"]'))
      .to.exist;
  });

  it('shows the last run with its status, subject and duration', async () => {
    const el = await card();
    await showTab(el, 'flows');
    const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('Succeeded');
    expect(text).to.not.contain('SUCCEEDED');
    expect(text).to.contain('preloop/preloop #352');
    expect(text).to.contain('3m 7s');
  });

  it('calls a run count the range when the range is what it counted', async () => {
    const el = await card();
    await showTab(el, 'flows');
    const cells = Array.from(el.shadowRoot!.querySelectorAll('td.num'));
    const runs = cells.find((td) => td.getAttribute('data-label') === 'Runs');
    const failed = cells.find(
      (td) => td.getAttribute('data-label') === 'Failed'
    );
    expect(runs?.getAttribute('title')).to.equal('In the last 30d');
    expect(failed?.getAttribute('title')).to.equal('In the last 30d');

    const empty = await card({
      flowRows: [flowRow({ lastRun: null, runs: 0, failed: 0 })],
    });
    await showTab(empty, 'flows');
    expect(empty.shadowRoot?.textContent).to.contain('No run in range');
  });

  it('names an agent before its usage lands, and holds the sort until then', async () => {
    const el = await card({
      usageLoading: true,
      agentRows: [
        agentRow(),
        agentRow({
          id: 'agent-2',
          name: 'Quiet Agent',
          requests: 9000,
          tokens: 90000,
          cost: 99,
          lastSeenAt: new Date(Date.now() - 86400000).toISOString(),
        }),
      ],
    });

    const cells = Array.from(
      el.shadowRoot!.querySelectorAll('tbody tr')[0].querySelectorAll('td')
    );
    expect(cells[0].textContent).to.contain('Hermes');
    expect(cells[2].querySelector('sl-skeleton'), 'Tokens').to.exist;
    expect(cells[3].querySelector('sl-skeleton'), '$ est.').to.exist;
    // Last seen comes off the agent itself, so it is never a skeleton.
    expect(cells[4].textContent!.trim()).to.not.equal('');

    (el as any).setSort('spend');
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelectorAll('tbody tr')[0].textContent
    ).to.contain('Hermes');

    (el as any).usageLoading = false;
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelectorAll('tbody tr')[0].textContent
    ).to.contain('Quiet Agent');
    expect(el.shadowRoot!.querySelector('sl-skeleton')).to.not.exist;
  });

  it('names a flow before its runs land, without turning the row into a skeleton', async () => {
    const el = await card({
      usageLoadingFlows: true,
      flowRows: [flowRow({ runs: 0, failed: 0, cost: 0, lastRun: null })],
    });
    await showTab(el, 'flows');

    const row = el.shadowRoot!.querySelector('tbody tr')!;
    expect(row.textContent).to.contain('Pull Request Reviewer');
    expect(row.querySelectorAll('sl-skeleton').length).to.be.greaterThan(0);
    const runsCell = row.querySelector('td[data-label="Runs"]')!;
    expect(runsCell.querySelector('sl-skeleton'), 'runs still pending').to
      .exist;
    expect(
      runsCell.querySelector('a'),
      'pending runs cell is not a nameless link'
    ).to.not.exist;
    expect(el.shadowRoot?.textContent).to.not.contain('No run in range');
    expect(el.shadowRoot?.querySelectorAll('.skeleton-row').length).to.equal(0);

    const sort = el.shadowRoot!.querySelector('sl-select.sort-select')!;
    expect(sort.hasAttribute('disabled'), 'sort held until usage').to.be.true;
    expect(sort.getAttribute('title')).to.equal(
      'Sort is held until usage arrives'
    );

    (el as any).usageLoadingFlows = false;
    (el as any).flowRows = [flowRow()];
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('tbody tr')!.textContent).to.contain(
      'Pull Request Reviewer'
    );
    expect(el.shadowRoot!.querySelector('sl-skeleton')).to.not.exist;
    expect(el.shadowRoot?.textContent).to.contain('12');
    expect(
      el
        .shadowRoot!.querySelector('td[data-label="Runs"] a')
        ?.getAttribute('href')
    ).to.equal('/console/flows/executions?flow_id=flow-1');
    expect(sort.hasAttribute('disabled')).to.be.false;
  });

  it('fills Runs when executions land while $ still waits on the breakdown', async () => {
    const el = await card({
      usageLoadingFlows: false,
      usageLoadingFlowCost: true,
      flowRows: [flowRow({ runs: 12, failed: 1, cost: 0 })],
    });
    await showTab(el, 'flows');
    const row = el.shadowRoot!.querySelector('tbody tr')!;
    const runsCell = row.querySelector('td[data-label="Runs"]')!;
    expect(runsCell.querySelector('a')).to.exist;
    expect(runsCell.querySelector('sl-skeleton')).to.not.exist;
    expect(row.querySelector('sl-skeleton'), 'cost still pending').to.exist;
  });

  it('names a model and a tool before their usage lands', async () => {
    const el = await card({
      usageLoadingModels: true,
      usageLoadingTools: true,
    });
    await showTab(el, 'models');
    const modelRowEl = el.shadowRoot!.querySelector('tbody tr')!;
    expect(modelRowEl.textContent).to.contain('claude-sonnet-4');
    expect(modelRowEl.querySelectorAll('sl-skeleton').length).to.equal(3);
    expect(el.shadowRoot?.querySelectorAll('.skeleton-row').length).to.equal(0);

    await showTab(el, 'tools');
    const toolRowEl = el.shadowRoot!.querySelector('tbody tr')!;
    expect(toolRowEl.textContent).to.contain('Bash');
    expect(toolRowEl.querySelectorAll('sl-skeleton').length).to.equal(2);

    (el as any).usageLoadingModels = false;
    (el as any).usageLoadingTools = false;
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('sl-skeleton')).to.not.exist;
  });

  it('says which layer broke the last run when the server categorised it', async () => {
    const el = await card({
      flowRows: [
        flowRow({
          lastRun: {
            id: 'exec-9',
            status: 'FAILED',
            start_time: new Date(Date.now() - 300000).toISOString(),
            end_time: new Date(Date.now() - 290000).toISOString(),
            failure_category: 'runner_conflict',
          },
        }),
      ],
    });
    await showTab(el, 'flows');

    const chips = Array.from(
      el.shadowRoot!.querySelectorAll('.last-run sl-badge')
    ).map((chip) => (chip.textContent || '').trim());
    expect(chips).to.eql(['Failed', 'Runner conflict']);

    // Nothing extra on a server that does not derive the category.
    const plain = await card();
    await showTab(plain, 'flows');
    expect(
      Array.from(plain.shadowRoot!.querySelectorAll('.last-run sl-badge')).map(
        (chip) => (chip.textContent || '').trim()
      )
    ).to.eql(['Succeeded']);
  });

  it('says the counts came off the last 100 runs when they did', async () => {
    const el = await card({ flowRunsCapped: true });
    await showTab(el, 'flows');
    const runs = Array.from(el.shadowRoot!.querySelectorAll('td.num')).find(
      (td) => td.getAttribute('data-label') === 'Runs'
    );
    expect(runs?.getAttribute('title')).to.equal(
      'In the last 30d, from the most recent 100 runs'
    );

    // "No run in range" would be a claim the page cannot make: the range
    // reaches further back than the hundred runs it read.
    const empty = await card({
      flowRunsCapped: true,
      flowRows: [flowRow({ lastRun: null, runs: 0, failed: 0 })],
    });
    await showTab(empty, 'flows');
    const text = (empty.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('No run in the most recent 100');
    expect(text).to.not.contain('No run in range');
  });

  it('claims no per-model failure count it cannot stand behind', async () => {
    const el = await card();
    await showTab(el, 'models');
    const headings = Array.from(el.shadowRoot!.querySelectorAll('th')).map(
      (th) => (th.textContent || '').trim()
    );
    expect(headings).to.eql([
      'Model',
      'Provider',
      'Requests',
      'Tokens',
      '$ est.',
    ]);
  });

  it('drops the provider prefix the next column already prints', async () => {
    // At 1440 the cell read "deepseek/deepse...", half of it spent saying
    // "DeepSeek" to the left of a column that says DeepSeek.
    const el = await card({
      modelRows: [
        modelRow({
          id: 'model-ds',
          alias: 'deepseek/deepseek-v4-pro',
          provider: 'DeepSeek',
        }),
        modelRow({
          id: 'model-or',
          alias: 'anthropic/claude-sonnet-4',
          provider: 'OpenRouter',
        }),
      ],
    });
    await showTab(el, 'models');
    const names = Array.from(
      el.shadowRoot!.querySelectorAll('tbody a.row-name')
    ).map((a) => (a.textContent || '').trim());
    expect(names).to.eql([
      'deepseek-v4-pro',
      // A prefix that is not this provider is part of the name.
      'anthropic/claude-sonnet-4',
    ]);
    const cell = el.shadowRoot!.querySelector('tbody td.identity-cell');
    expect(cell?.getAttribute('title')).to.equal('deepseek/deepseek-v4-pro');
  });

  it('gives the model name the width and the counts the gutters', async () => {
    const el = await card();
    await showTab(el, 'models');
    const widths = Array.from(el.shadowRoot!.querySelectorAll('col')).map(
      (col) => col.getAttribute('style')
    );
    expect(widths[0]).to.contain('42%');
    expect(widths.slice(2).every((style) => style?.includes('12%'))).to.equal(
      true
    );
  });

  it('keeps an old timestamp relative, with the date in the title', async () => {
    const stale = new Date(Date.now() - 45 * 86400000).toISOString();
    const el = await card({ agentRows: [agentRow({ lastSeenAt: stale })] });
    const cell = el.shadowRoot!.querySelector('tbody tr td:last-child');
    expect((cell?.textContent || '').trim()).to.equal('6w ago');
    expect(cell?.getAttribute('title')).to.contain(
      String(new Date(stale).getFullYear())
    );
  });

  it('labels every number the phone line prints, and leads the separators', async () => {
    const el = await card();
    const labels = Array.from(
      el.shadowRoot!.querySelectorAll('tbody tr td.num')
    ).map((cell) => cell.getAttribute('data-label'));
    // Spend and last seen used to arrive as two bare numbers with no word
    // saying what they were.
    expect(labels).to.deep.equal(['Tokens', 'Spend', 'Seen']);

    // Read from the stylesheet, not the DOM: the separator is a ::before on
    // a media query, and a pseudo element has no node to query. The cost is
    // that an equivalent rewrite of the rule breaks this assertion even
    // though the behaviour holds.
    const css = Array.from(el.shadowRoot!.adoptedStyleSheets)
      .flatMap((sheet) =>
        Array.from(sheet.cssRules).map((rule) => rule.cssText)
      )
      .join('\n');
    // The dot leads the value it belongs to. Trailing it left a dot alone at
    // the end of a wrapped line, and left one behind when Tokens was hidden.
    expect(css).to.contain("content: '· ' attr(data-label) ' '");
    expect(css).to.not.contain("content: ' ·'");
  });

  it('says what is missing, with the way to fix it', async () => {
    const el = await card({
      agentRows: [],
      flowRows: [],
      modelRows: [],
      toolRows: [],
    });
    await showTab(el, 'flows');
    const empty = el.shadowRoot?.querySelector('.empty');
    expect((empty?.textContent || '').replace(/\s+/g, ' ').trim()).to.contain(
      'No flows yet.'
    );
    expect(empty?.querySelector('a')?.getAttribute('href')).to.equal(
      '/console/flows/new'
    );
    expect(el.shadowRoot?.querySelector('table')).to.not.exist;
  });

  it('renders the tab strip with counts while the rows are still loading', async () => {
    const el = await card({
      agentRows: [],
      flowRows: [],
      modelRows: [],
      toolRows: [],
      loading: true,
    });
    expect(tabText(el)[0]).to.equal('Agents 10');
    expect(
      el.shadowRoot?.querySelectorAll('sl-skeleton').length
    ).to.be.greaterThan(0);
    expect(el.shadowRoot?.querySelector('.empty')).to.not.exist;
  });

  it('lets one tab wait while another already has its rows', async () => {
    // Agents arrive with the first wave, flows a moment later. One flag for
    // the whole card kept the rows it already had behind a skeleton.
    const el = await fixture<InventoryCard>(html`
      <inventory-card
        .agentRows=${[agentRow()]}
        .flowRows=${[]}
        .modelRows=${[]}
        .toolRows=${[]}
        .agentsTotal=${10}
        .flowsTotal=${30}
        .modelsTotal=${16}
        .toolsTotal=${16}
        .loadingAgents=${false}
        .loadingFlows=${true}
        rangeLabel="30d"
      ></inventory-card>
    `);
    await el.updateComplete;

    expect(el.shadowRoot?.querySelectorAll('tbody tr').length).to.equal(1);
    expect(el.shadowRoot?.querySelector('sl-skeleton')).to.not.exist;

    await showTab(el, 'flows');
    expect(
      el.shadowRoot?.querySelectorAll('sl-skeleton').length,
      'flows still waiting'
    ).to.be.greaterThan(0);
    expect(el.shadowRoot?.querySelector('.empty'), 'no premature empty state')
      .to.not.exist;
  });

  it('caps the table and points at the full list', async () => {
    const rows = Array.from({ length: 12 }, (_, index) =>
      agentRow({ id: `agent-${index}`, name: `Agent ${index}` })
    );
    const el = await card({ agentRows: rows });
    const shown = el.shadowRoot!.querySelectorAll('tbody tr').length;
    // Six rows on a laptop, eight when the viewport can hold them: the card
    // is sized by the screen, not by how much data happens to exist.
    expect(shown).to.equal(window.innerHeight > 1000 ? 8 : 6);

    const footer = el.shadowRoot?.querySelector('.footer a');
    expect((footer?.textContent || '').trim()).to.equal('View all 10 agents →');
    expect(footer?.getAttribute('href')).to.equal('/console/agents');
  });

  it('restates the page range as a muted label, with no select of its own', async () => {
    // One range per page: the Usage card owns it, the inventory says it.
    const el = await card({ timeRange: 'this-week' });
    expect(el.shadowRoot?.querySelector('time-range-select')).to.not.exist;
    const label = el.shadowRoot?.querySelector('.range-label');
    expect(label, 'range label').to.exist;
    expect(label!.textContent!.trim()).to.equal('This week');
    expect(label!.getAttribute('title')).to.contain('Usage');
    expect(el.shadowRoot?.querySelector('.sort-select')).to.exist;
  });

  it('ends the header controls at the range label, with no gap', async () => {
    const el = await card();
    const controls = el.shadowRoot!.querySelector('.header-controls')!;
    const period = el.shadowRoot!.querySelector('.range-label')!;
    const gap =
      controls.getBoundingClientRect().right -
      period.getBoundingClientRect().right;
    expect(gap).to.be.lessThan(1);
  });

  it('keeps the header controls inside the card on a phone', async () => {
    const el = await card();
    el.style.display = 'block';
    el.style.width = '320px';
    await el.updateComplete;
    const head = el.shadowRoot!.querySelector('.card-head')!;
    for (const control of el.shadowRoot!.querySelectorAll(
      '.header-controls > *'
    )) {
      expect(control.getBoundingClientRect().right).to.be.at.most(
        head.getBoundingClientRect().right + 1
      );
    }
  });

  it('opens a phone tab strip on the remembered tab with room on both sides', async () => {
    localStorage.setItem(INVENTORY_TAB_STORAGE_KEY, 'devices');
    const wrapper = await fixture<HTMLDivElement>(
      html`<div style="width: 320px"></div>`
    );
    const el = document.createElement('inventory-card') as InventoryCard;
    Object.assign(el, {
      agentRows: [agentRow()],
      showUsers: true,
      usersTotal: 28,
      showDevices: true,
      devicesTotal: 4,
      agentsTotal: 10,
      flowsTotal: 30,
      modelsTotal: 16,
      toolsTotal: 16,
    });
    wrapper.appendChild(el);
    await el.updateComplete;
    const group = el.shadowRoot!.querySelector(
      'sl-tab-group'
    ) as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await group.updateComplete;
    // No scroll buttons: they clipped the previous tab into a "< 28" fragment.
    expect(group.hasAttribute('no-scroll-controls')).to.be.true;
    const nav = group.shadowRoot!.querySelector(
      '.tab-group__nav'
    ) as HTMLElement;
    const active = el.shadowRoot!.querySelector(
      'sl-tab[panel="devices"]'
    ) as HTMLElement;
    await waitUntil(() => nav.scrollLeft > 0, 'strip scrolls to the tab');
    await new Promise((resolve) => setTimeout(resolve, 400));
    const navBox = nav.getBoundingClientRect();
    const tabBox = active.getBoundingClientRect();
    expect(tabBox.right).to.be.at.most(navBox.right);
    expect(tabBox.left - navBox.left).to.be.at.least(16);
  });

  it('has no Map view: the fleet map lives on its own page', async () => {
    const el = await card({ showDevices: true, devicesTotal: 1 });
    expect(el.shadowRoot?.querySelector('.view-switch')).to.not.exist;
    expect(el.shadowRoot?.querySelector('fleet-map')).to.not.exist;
  });
  describe('users tab', () => {
    const withUsers = (props = {}) =>
      card({
        showUsers: true,
        usersTotal: 4,
        userRows: [
          userRow(),
          userRow({
            id: 'user-2',
            name: 'Grace Hopper',
            role: 'Member',
            lastLoginAt: new Date(Date.now() - 86400000 * 3).toISOString(),
            agentsOwned: 1,
            tokens: 12000,
            cost: 41.25,
          }),
        ],
        ...props,
      });

    it('is absent without user management', async () => {
      const el = await card();
      expect(tabText(el)).to.eql([
        'Agents 10',
        'Flows 30',
        'Models 16',
        'Tools 16',
      ]);
    });

    it('lists who is on the account, what they own and what they spent', async () => {
      const el = await withUsers();
      expect(tabText(el)[4]).to.equal('Users 4');

      await showTab(el, 'users');
      const rows = el.shadowRoot!.querySelectorAll('tbody tr');
      expect(rows.length).to.equal(2);
      const first = rows[0].textContent!.replace(/\s+/g, ' ');
      expect(first).to.contain('Ada Lovelace');
      expect(first).to.contain('Admin');
      expect(first).to.contain('2h ago');
      expect(first).to.contain('3');
      expect(first).to.contain('$9.50');
      expect(rows[0].querySelector('user-avatar')).to.exist;
    });

    it('sorts by spend when asked, not just by last login', async () => {
      const el = await withUsers();
      await showTab(el, 'users');
      expect(el.shadowRoot!.querySelector('tbody tr')!.textContent).to.contain(
        'Ada Lovelace'
      );

      (el as any).setSort('spend');
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('tbody tr')!.textContent).to.contain(
        'Grace Hopper'
      );
    });

    it('shows who is on the account before their spend has arrived', async () => {
      const el = await withUsers({ usageLoading: true });
      await showTab(el, 'users');

      const cells = Array.from(
        el.shadowRoot!.querySelectorAll('tbody tr')[0].querySelectorAll('td')
      );
      // Identity is known the moment the users list lands.
      expect(cells[0].textContent).to.contain('Ada Lovelace');
      expect(cells[1].textContent).to.contain('Admin');
      expect(cells[2].textContent).to.contain('2h ago');
      expect(cells[3].textContent!.trim()).to.equal('3');
      // Usage is not, and says so instead of printing a zero.
      expect(cells[4].querySelector('sl-skeleton')).to.exist;
      expect(cells[5].querySelector('sl-skeleton')).to.exist;
      expect(cells[5].textContent).to.not.contain('$');
    });

    it('waits for the usage before sorting by spend', async () => {
      const el = await withUsers({ usageLoading: true });
      await showTab(el, 'users');
      (el as any).setSort('spend');
      await el.updateComplete;

      // Grace outspends Ada, but nothing here knows that yet: the rows keep
      // the order the identity sort gave them rather than sorting by zero.
      expect(el.shadowRoot!.querySelector('tbody tr')!.textContent).to.contain(
        'Ada Lovelace'
      );

      (el as any).usageLoading = false;
      await el.updateComplete;
      expect(el.shadowRoot!.querySelector('tbody tr')!.textContent).to.contain(
        'Grace Hopper'
      );
    });

    // One person on the account is not a list to open.
    it('asks a lone user to invite somebody instead of viewing all 1', async () => {
      const el = await withUsers({
        usersTotal: 1,
        userRows: [userRow()],
      });
      await showTab(el, 'users');

      const footer = el.shadowRoot!.querySelector('.footer')!;
      expect(footer.textContent!.replace(/\s+/g, ' ')).to.contain(
        'Working alone? Invite a teammate'
      );
      expect(footer.querySelector('a')!.getAttribute('href')).to.equal(
        '/console/settings/invitations'
      );
    });

    it('falls back to Agents when a remembered Users tab is not offered', async () => {
      localStorage.setItem(INVENTORY_TAB_STORAGE_KEY, 'users');
      const el = await card();

      expect(el.shadowRoot?.querySelector('a[href="/console/agents/agent-1"]'))
        .to.exist;
    });
  });

  describe('devices tab', () => {
    const withDevices = (props = {}) =>
      card({
        showDevices: true,
        devicesTotal: 3,
        deviceRows: [
          deviceRow(),
          deviceRow({
            key: 'fp:bbbb',
            name: 'maya-laptop',
            os: 'macOS',
            href: '/console/settings/device-fleet?device=fp%3Abbbb',
            people: [],
            runnerState: 'offline',
            runnerLabel: 'Runner offline',
            runnerLive: false,
            agentsGoverned: 4,
            agentsUngoverned: 0,
            lastSeenAt: new Date(Date.now() - 86400000 * 2).toISOString(),
          }),
        ],
        ...props,
      });

    it('is absent without device fleet access', async () => {
      const el = await card();
      expect(tabText(el).some((label) => label.startsWith('Devices'))).to.be
        .false;
    });

    it('comes last, after Users, with its count', async () => {
      const el = await withDevices({ showUsers: true, usersTotal: 3 });
      const labels = tabText(el);
      expect(labels.at(-1)).to.equal('Devices 3');
      expect(labels.at(-2)).to.match(/^Users/);
    });

    it('keeps the range label and the Last seen column', async () => {
      const el = await withDevices();
      await showTab(el, 'devices');
      const root = el.shadowRoot!;
      expect(root.querySelector('.range-label')).to.exist;
      const headers = Array.from(root.querySelectorAll('th')).map((th) =>
        th.textContent?.trim()
      );
      expect(headers).to.include('Last seen');
      expect(
        root.querySelectorAll('tbody td[data-label="Last seen"]').length
      ).to.equal(2);
    });

    it('links each device to its fleet page and each person to theirs', async () => {
      const el = await withDevices();
      await showTab(el, 'devices');
      const root = el.shadowRoot!;
      const headers = Array.from(root.querySelectorAll('th')).map((th) =>
        th.textContent?.trim()
      );
      expect(headers).to.eql([
        'Device',
        'People',
        'Runner',
        'Agents',
        'Last seen',
      ]);
      expect(
        root.querySelector(
          'a.row-name[href="/console/settings/device-fleet?device=fp%3Aaaaa"]'
        )?.textContent
      ).to.equal('build-01');
      expect(
        root.querySelector('a[href="/console/settings/users/user-1"]')
          ?.textContent
      ).to.equal('Jane Doe');
      const people = root.querySelector('.device-people') as HTMLElement;
      expect(people.textContent).to.contain('+1');
      expect(people.title).to.equal('Jane Doe, jdoe@example.com');
      // Most recently seen first, and no link for an unknown person.
      const rows = root.querySelectorAll('tbody tr');
      expect(rows[0].textContent).to.contain('build-01');
      expect(rows[1].textContent).to.contain('Unknown');
      expect(rows[1].textContent).to.contain('Runner offline');
      expect(
        (rows[0].querySelector('td[data-label="Agents"]') as HTMLElement).title
      ).to.equal('2 governed, 1 not onboarded');
      expect(root.querySelector('.footer a')?.getAttribute('href')).to.equal(
        '/console/settings/device-fleet'
      );
    });

    it('sorts by agent count on request', async () => {
      const el = await withDevices();
      await showTab(el, 'devices');
      el['setSort']('agents');
      await el.updateComplete;
      const first = el.shadowRoot!.querySelector('tbody tr');
      expect(first?.textContent).to.contain('maya-laptop');
    });

    it('skeletons while loading and says so when empty', async () => {
      const loading = await withDevices({
        deviceRows: [],
        loadingDevices: true,
      });
      await showTab(loading, 'devices');
      expect(loading.shadowRoot!.querySelector('.skeleton-row')).to.exist;

      const empty = await withDevices({ deviceRows: [], devicesTotal: 0 });
      await showTab(empty, 'devices');
      expect(empty.shadowRoot!.querySelector('.empty')?.textContent).to.contain(
        'No devices yet.'
      );
    });
  });

  it('says a tab could not be loaded instead of claiming zero', async () => {
    const el = await card({ agentRows: [], unavailableTabs: ['agents'] });
    const root = el.shadowRoot!;
    expect(tabText(el)[0]).to.equal('Agents');
    expect(root.querySelector('.empty')?.textContent).to.contain(
      'Agents could not be loaded'
    );
    expect(root.querySelector('.empty')?.textContent).to.not.contain(
      'No agents yet'
    );
    expect(root.querySelector('.footer')).to.not.exist;
  });

  it('links a teammate in the Users tab to their person page', async () => {
    const el = await card({
      showUsers: true,
      usersTotal: 2,
      userRows: [userRow()],
    });
    await showTab(el, 'users');
    expect(
      el.shadowRoot!.querySelector(
        'a.row-name[href="/console/settings/users/user-1"]'
      )?.textContent
    ).to.equal('Ada Lovelace');
  });
});

describe('inventory-card Agents columns', () => {
  const LIST = 'overview-inventory-agents';
  afterEach(() => clearTablePreferences(LIST));

  const headers = (el: HTMLElement) =>
    Array.from(el.shadowRoot!.querySelectorAll('table thead th')).map((th) =>
      (th.textContent || '').trim()
    );

  it('keeps long model aliases readable in a constrained inventory card', async () => {
    const el = await card({
      agentRows: [
        agentRow({
          modelAlias:
            'provider/research-model-with-a-long-distinguishing-suffix',
        }),
      ],
    });
    el.style.width = '735px';
    await el.updateComplete;
    const model = el.shadowRoot!.querySelector(
      '.inventory-model'
    ) as HTMLElement;
    expect(model.getBoundingClientRect().width).to.be.at.least(100);
    expect(model.textContent).to.contain('distinguishing-suffix');
    const text = model.querySelector('span')!;
    expect(getComputedStyle(text).whiteSpace).to.equal('normal');
    expect(model.scrollWidth).to.be.at.most(model.clientWidth + 1);
  });

  it('lets the column picker switch a column off, and remembers it', async () => {
    const el = await card({ agentRows: [agentRow()] });
    expect(headers(el)).to.include('Tokens');
    const picker = el.shadowRoot!.querySelector(
      'thead tr th:last-child column-picker'
    ) as HTMLElement & {
      columns: Array<{ id: string; hideable?: boolean }>;
    };
    expect(picker).to.exist;
    expect(
      el.shadowRoot!.querySelector(
        'list-toolbar column-picker, .toolbar column-picker'
      )
    ).to.be.null;
    // The name identifies the row; the picker may not take it away.
    expect(picker.columns.find((c) => c.id === 'name')?.hideable).to.be.false;
    picker.dispatchEvent(
      new CustomEvent('column-toggle', {
        detail: { id: 'tokens', visible: false },
        bubbles: true,
        composed: true,
      })
    );
    await el.updateComplete;
    expect(headers(el)).to.not.include('Tokens');
    expect(readTablePreferences(LIST)?.columnVisibility?.tokens).to.equal(
      false
    );
  });

  it('keeps the last heading readable beside the column picker', async () => {
    const el = await card({ agentRows: [agentRow()] });
    const cell = el.shadowRoot!.querySelector(
      'table thead th:last-child'
    ) as HTMLElement;
    const label = cell.querySelector('.header-label') as HTMLElement;
    const picker = cell.querySelector('column-picker') as HTMLElement;
    const trigger = picker.shadowRoot!.querySelector(
      'sl-button[slot="trigger"]'
    ) as HTMLElement;
    expect(label.textContent!.trim()).to.equal('Last seen');
    expect(label.scrollWidth).to.be.at.most(label.clientWidth + 1);
    const cellBox = cell.getBoundingClientRect();
    const labelBox = label.getBoundingClientRect();
    const triggerBox = trigger.getBoundingClientRect();
    expect(labelBox.left).to.be.at.least(cellBox.left - 0.5);
    expect(labelBox.right).to.be.at.most(triggerBox.left + 0.5);
    expect(triggerBox.right).to.be.at.most(cellBox.right + 0.5);
  });

  it('gives a numeric header the same narrow gutter as its numbers', async () => {
    const el = await card({ agentRows: [agentRow()] });
    const header = el.shadowRoot!.querySelector(
      'table thead th.numeric'
    ) as HTMLElement;
    const cell = el.shadowRoot!.querySelector('tbody td.num') as HTMLElement;
    expect(header).to.exist;
    expect(getComputedStyle(header).textAlign).to.equal('right');
    expect(getComputedStyle(header).paddingLeft).to.equal(
      getComputedStyle(cell).paddingLeft
    );
  });

  it('gives the agent name the widest column and a floor', async () => {
    const el = (await card({
      agentRows: [agentRow({ name: 'a-very-long-agent-name-on-a-build-host' })],
    })) as HTMLElement & {
      agentTable: {
        widthOfColumn(id: string): number;
        resizeBy(id: string, d: number): void;
      };
    };
    const table = el.agentTable;
    expect(table.widthOfColumn('name')).to.equal(212);
    table.resizeBy('name', -1000);
    await el.updateComplete;
    expect(table.widthOfColumn('name')).to.equal(160);
    const link = el.shadowRoot!.querySelector('a.row-name') as HTMLElement;
    expect(link.getAttribute('title')).to.equal(
      'a-very-long-agent-name-on-a-build-host'
    );
  });

  it('keeps the without-subscriptions figure in the spend title, not a second line', async () => {
    const el = await card({
      agentRows: [
        agentRow({
          cost: 0.74,
          withoutSubscriptions: {
            value: 6197.29,
            partial: true,
            text: 'at least $6,197.29',
            title: '2 of 3 subscription requests carry an API-equivalent price',
          },
          subscriptionRequests: 3,
        }),
      ],
    });
    const cell = el.shadowRoot!.querySelector(
      'td[data-label="Spend"]'
    ) as HTMLElement;
    expect(cell.querySelector('[data-testid="without-subscriptions"]')).to.not
      .exist;
    expect((cell.textContent || '').trim()).to.equal('$0.74');
    const title = cell.querySelector('span[title]')!.getAttribute('title')!;
    expect(title).to.contain('Without subscriptions: at least $6,197.29');
    expect(title).to.contain('2 of 3 subscription requests');
  });

  it('keeps a resized width across reloads', async () => {
    const el = await card({ agentRows: [agentRow()] });
    const handle = el.shadowRoot!.querySelector(
      'th.col-name .col-resize'
    ) as HTMLElement;
    expect(handle).to.exist;
    handle.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })
    );
    await el.updateComplete;
    expect(readTablePreferences(LIST)?.columnSizing?.name).to.equal(228);

    const again = (await card({ agentRows: [agentRow()] })) as HTMLElement & {
      agentTable: { widthOfColumn(id: string): number };
    };
    expect(again.agentTable.widthOfColumn('name')).to.equal(228);
  });
});

describe('inventory-card spatial map', () => {
  let api: MockApi;
  afterEach(() => {
    api?.restore();
    localStorage.removeItem('accessToken');
    for (const key of Object.keys(localStorage))
      if (
        key.startsWith('preloop:inventory:') ||
        key.startsWith('preloop:fleet-map:')
      )
        localStorage.removeItem(key);
  });

  beforeEach(() => localStorage.removeItem('preloop:fleet-map:window'));

  it('offers no map to anyone but an instance administrator', async () => {
    localStorage.setItem('preloop:inventory:view:u1', 'map');
    const el = await fixture<InventoryCard>(
      html`<inventory-card userId="u1"></inventory-card>`
    );
    expect(el.shadowRoot!.querySelector('[data-inventory-view]')).to.not.exist;
    expect(el.shadowRoot!.querySelector('fleet-map')).to.not.exist;
  });

  it('swaps the table for the spatial fleet map and remembers it', async () => {
    localStorage.setItem('accessToken', 'test-token');
    api = mockApi({
      routes: [
        {
          path: '/api/v1/fleet/map',
          body: { ...sampleFleetMap(), active_within_minutes: 43200 },
        },
      ],
    });
    const el = await fixture<InventoryCard>(
      html`<inventory-card instanceAdmin userId="u1"></inventory-card>`
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-inventory-view="map"]'
    )!.click();
    await waitUntil(() => el.shadowRoot!.querySelector('fleet-map')?.graph);
    const map = el.shadowRoot!.querySelector('fleet-map')!;
    await map.updateComplete;
    expect(map.positionsScope).to.equal('inventory');
    expect(map.shadowRoot!.querySelector('[data-node="agent:codex"]')).to.exist;
    // The live spatial canvas is the default; the columns are one click away.
    expect(map.shadowRoot!.querySelector('.column-title')).to.not.exist;
    map
      .shadowRoot!.querySelector<HTMLButtonElement>(
        '[data-arrangement="columns"]'
      )!
      .click();
    await map.updateComplete;
    expect(map.shadowRoot!.querySelector('.column-title')).to.exist;
    expect(
      localStorage.getItem('preloop:fleet-map:arrangement:inventory:u1')
    ).to.equal('columns');
    expect(el.shadowRoot!.querySelector('table')).to.not.exist;
    expect(localStorage.getItem('preloop:inventory:view:u1')).to.equal('map');
    expect(api.callsTo('/api/v1/fleet/map').length).to.equal(1);
  });

  it('refetches the map scoped to the users chosen on it', async () => {
    localStorage.setItem('accessToken', 'test-token');
    api = mockApi({
      routes: [{ path: '/api/v1/fleet/map', body: sampleFleetMap() }],
    });
    const el = await fixture<InventoryCard>(
      html`<inventory-card instanceAdmin userId="u9"></inventory-card>`
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-inventory-view="map"]'
    )!.click();
    await waitUntil(() => el.shadowRoot!.querySelector('fleet-map')?.graph);
    const map = el.shadowRoot!.querySelector('fleet-map')!;
    await map.updateComplete;
    map
      .shadowRoot!.querySelector<HTMLElement>('[data-user="user:sam"]')!
      .click();
    await waitUntil(() => api.callsTo('/api/v1/fleet/map').length === 2);
    expect(api.callsTo('/api/v1/fleet/map')[1].search).to.contain('users=sam');
    await el.updateComplete;
    expect(map.users).to.deep.equal(['user:sam']);
    localStorage.removeItem('preloop:inventory:view:u9');
  });

  it('keeps the table the default for an administrator', async () => {
    const el = await fixture<InventoryCard>(
      html`<inventory-card instanceAdmin userId="u2"></inventory-card>`
    );
    expect(
      el
        .shadowRoot!.querySelector('[data-inventory-view="table"]')!
        .getAttribute('aria-pressed')
    ).to.equal('true');
    expect(el.shadowRoot!.querySelector('fleet-map')).to.not.exist;
  });

  it('shows the live map with pull request counters from the endpoint', async () => {
    localStorage.setItem('accessToken', 'test-token');
    localStorage.setItem('preloop:inventory:view:u1', 'map');
    api = mockApi({
      routes: [
        {
          path: '/api/v1/fleet/map',
          body: { ...sampleFleetMap(), active_within_minutes: 43200 },
        },
        {
          path: '/api/v1/fleet/code-activity',
          body: {
            active_within_minutes: 43200,
            since: new Date().toISOString(),
            truncated: false,
            counts: {
              'agent:codex': {
                opened: 3,
                commented: 0,
                reviewed: 0,
                merged: 1,
              },
            },
            flows: [],
            events: [],
          },
        },
      ],
    });
    const el = await fixture<InventoryCard>(
      html`<inventory-card instanceAdmin userId="u1"></inventory-card>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('fleet-map')?.graph);
    const map = el.shadowRoot!.querySelector('fleet-map')!;
    expect(map.live).to.equal(true);
    await waitUntil(
      () => map.shadowRoot!.querySelector('[data-code="agent:codex"]'),
      'code counters render',
      { timeout: 3000 }
    );
    const badges = [
      ...map.shadowRoot!.querySelectorAll<HTMLElement>(
        '[data-code="agent:codex"] .code-badge'
      ),
    ].map((b) => `${b.dataset.action}:${b.textContent!.trim()}`);
    expect(badges).to.deep.equal(['opened:3', 'merged:1']);
    expect(
      api.callsTo('/api/v1/fleet/code-activity')[0]?.search ?? ''
    ).to.contain('active_within_minutes=43200');
  });
});

describe('inventory-card agents table at 1440 (B-07)', () => {
  beforeEach(() => {
    localStorage.removeItem(INVENTORY_TAB_STORAGE_KEY);
  });

  it('fits the card with Last seen and Model visible', async () => {
    // The Overview main column is 735px wide at a 1440 viewport.
    const host = await fixture<HTMLElement>(
      html`<div style="width: 735px">
        <inventory-card
          .agentRows=${[
            agentRow({
              name: 'A coding agent with a long display name',
              lastSeenAt: new Date(Date.now() - 26 * 3600_000).toISOString(),
            }),
          ]}
          .agentsTotal=${1}
          rangeLabel="30d"
        ></inventory-card>
      </div>`
    );
    const el = host.querySelector('inventory-card') as InventoryCard;
    await el.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 50));
    const table = el.shadowRoot!.querySelector(
      'table.inventory-table'
    ) as HTMLElement;
    const scroll = table.parentElement as HTMLElement;
    expect(table.scrollWidth, 'the table fits its card').to.be.at.most(
      scroll.clientWidth + 1
    );
    const model = table.querySelector('thead th.col-model') as HTMLElement;
    expect(model.getBoundingClientRect().width).to.be.at.least(56);
  });
});
