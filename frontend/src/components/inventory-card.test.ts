import { expect, fixture, html } from '@open-wc/testing';
import './inventory-card.ts';
import { INVENTORY_TAB_STORAGE_KEY } from './inventory-card';
import type {
  InventoryAgentRow,
  InventoryCard,
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

async function card(
  props: Partial<{
    agentRows: InventoryAgentRow[];
    flowRows: InventoryFlowRow[];
    modelRows: InventoryModelRow[];
    toolRows: InventoryToolRow[];
    userRows: InventoryUserRow[];
    usersTotal: number;
    showUsers: boolean;
    loading: boolean;
  }> = {}
): Promise<InventoryCard> {
  const el = await fixture<InventoryCard>(html`
    <inventory-card
      .agentRows=${props.agentRows ?? [agentRow()]}
      .flowRows=${props.flowRows ?? [flowRow()]}
      .modelRows=${props.modelRows ?? [modelRow()]}
      .toolRows=${props.toolRows ?? [toolRow()]}
      .userRows=${props.userRows ?? []}
      .usersTotal=${props.usersTotal ?? 0}
      ?showUsers=${props.showUsers ?? false}
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
    // Six rows on a laptop, eight when the viewport can hold them: the card
    // is sized by the screen, not by how much data happens to exist.
    expect(shown).to.equal(window.innerHeight > 1000 ? 8 : 6);

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
});
