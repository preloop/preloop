import { expect, fixture, html } from '@open-wc/testing';
import './inventory-card.ts';
import { INVENTORY_TAB_STORAGE_KEY } from './inventory-card';
import type {
  InventoryAgentRow,
  InventoryCard,
  InventoryFlowRow,
  InventoryModelRow,
  InventoryToolRow,
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
    failed: 2,
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

async function card(
  props: Partial<{
    agentRows: InventoryAgentRow[];
    flowRows: InventoryFlowRow[];
    modelRows: InventoryModelRow[];
    toolRows: InventoryToolRow[];
    loading: boolean;
  }> = {}
): Promise<InventoryCard> {
  const el = await fixture<InventoryCard>(html`
    <inventory-card
      .agentRows=${props.agentRows ?? [agentRow()]}
      .flowRows=${props.flowRows ?? [flowRow()]}
      .modelRows=${props.modelRows ?? [modelRow()]}
      .toolRows=${props.toolRows ?? [toolRow()]}
      .agentsTotal=${10}
      .flowsTotal=${30}
      .modelsTotal=${16}
      .toolsTotal=${16}
      .loading=${props.loading ?? false}
      rangeLabel="30d"
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

  it('carries the counts in the tab labels', async () => {
    const el = await card();
    expect(tabText(el)).to.eql([
      'Agents 10',
      'Flows 30',
      'Models 16',
      'Tools 16',
    ]);
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
    expect(options()).to.eql(['Spend', 'Requests', 'Failures']);
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
    expect(text).to.contain('SUCCEEDED');
    expect(text).to.contain('preloop/preloop #352');
    expect(text).to.contain('3m 7s');
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

  it('caps the table and points at the full list', async () => {
    const rows = Array.from({ length: 12 }, (_, index) =>
      agentRow({ id: `agent-${index}`, name: `Agent ${index}` })
    );
    const el = await card({ agentRows: rows });
    const shown = el.shadowRoot!.querySelectorAll('tbody tr').length;
    expect(shown).to.be.at.most(8);
    expect(shown).to.be.at.least(6);

    const footer = el.shadowRoot?.querySelector('.footer a');
    expect((footer?.textContent || '').trim()).to.equal('View all 10 agents →');
    expect(footer?.getAttribute('href')).to.equal('/console/agents');
  });

  it('states the page range without offering a second one', async () => {
    const el = await card();
    expect(
      (el.shadowRoot?.querySelector('.range-label')?.textContent || '').trim()
    ).to.equal('30d');
    expect(el.shadowRoot?.querySelector('time-range-select')).to.not.exist;
  });
});
