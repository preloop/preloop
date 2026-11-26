import { expect, fixture, html, aTimeout } from '@open-wc/testing';

import './discovered-agents-panel';
import {
  onboardCommandFor,
  shortWorkstation,
  type DiscoveredAgentsPanel,
} from './discovered-agents-panel';
import type { DiscoveredAgentCandidate, DiscoveryCandidatePage } from '../api';

function candidate(
  overrides: Partial<DiscoveredAgentCandidate> = {}
): DiscoveredAgentCandidate {
  return {
    id: 'c-1',
    agent_kind: 'cursor',
    agent_version: null,
    workstation_fingerprint: 'abcdef0123456789'.repeat(4),
    config_path_hash: '1'.repeat(64),
    mcp_server_count: 3,
    enrolled: false,
    os_family: 'darwin',
    status: 'new',
    managed_agent_id: null,
    first_seen_at: new Date(Date.now() - 86_400_000).toISOString(),
    last_seen_at: new Date().toISOString(),
    ...overrides,
  };
}

async function mount(
  rows: DiscoveredAgentCandidate[],
  updater?: DiscoveredAgentsPanel['updater'],
  page: Partial<DiscoveryCandidatePage> = {}
): Promise<DiscoveredAgentsPanel> {
  const loaded: DiscoveryCandidatePage = {
    items: rows,
    total: rows.length,
    truncated: false,
    ...page,
  };
  const el = await fixture<DiscoveredAgentsPanel>(
    html`<discovered-agents-panel
      .loader=${async () => loaded}
      .updater=${updater ?? (async () => rows[0])}
    ></discovered-agents-panel>`
  );
  // connectedCallback ran with the default loader before the property
  // binding landed; refresh with the stubbed one.
  await el.refresh();
  await el.updateComplete;
  return el;
}

describe('discovered-agents-panel', () => {
  it('renders the Not yet governed list with a short workstation hash', async () => {
    const el = await mount([
      candidate(),
      candidate({ id: 'c-2', agent_kind: 'claude_code' }),
    ]);
    const root = el.shadowRoot!;
    expect(root.querySelector('h2')?.textContent).to.contain(
      'Not yet governed'
    );
    expect(root.querySelector('sl-badge')?.textContent?.trim()).to.equal('2');
    expect(root.textContent).to.not.contain('Showing the first');
    const rows = root.querySelectorAll('tbody tr');
    expect(rows.length).to.equal(2);
    expect(rows[0].textContent).to.contain('cursor');
    expect(rows[0].querySelector('code')?.textContent?.trim()).to.equal(
      'abcdef01'
    );
    const copy = rows[1].querySelector('sl-copy-button');
    expect(copy?.getAttribute('value')).to.equal(
      'preloop agents onboard claude_code'
    );
    expect(rows[0].querySelector('sl-button.mark-ignored')).to.exist;
  });

  it('badges only candidates first seen in the last seven days and not onboarded', async () => {
    const el = await mount([
      candidate(),
      candidate({
        id: 'old',
        first_seen_at: new Date(Date.now() - 8 * 86_400_000).toISOString(),
      }),
      candidate({ id: 'enrolled', enrolled: true }),
      candidate({ id: 'linked', managed_agent_id: 'managed-1' }),
      candidate({
        id: 'future',
        first_seen_at: new Date(Date.now() + 86_400_000).toISOString(),
      }),
    ]);
    const badges = el.shadowRoot!.querySelectorAll('.new-discovery');
    expect(badges.length).to.equal(1);
    expect(badges[0].textContent).to.contain('New this week');
    expect(el.shadowRoot!.textContent).to.contain('First seen');
  });

  it('shows the full total when the list is capped', async () => {
    const el = await mount([candidate()], undefined, {
      total: 501,
      truncated: true,
    });
    const root = el.shadowRoot!;
    expect(root.querySelector('sl-badge')?.textContent?.trim()).to.equal('501');
    expect(root.querySelectorAll('tbody tr').length).to.equal(1);
    expect(root.textContent).to.contain('Showing the first 1 of 501');
  });

  it('renders nothing when no candidates were reported', async () => {
    const el = await mount([]);
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });

  it('removes a row after Mark ignored', async () => {
    const calls: Array<[string, string]> = [];
    const el = await mount([candidate()], async (id, status) => {
      calls.push([id, status]);
      return candidate({ status: 'ignored' });
    });
    (
      el.shadowRoot!.querySelector('sl-button.mark-ignored') as HTMLElement
    ).click();
    await aTimeout(0);
    await el.updateComplete;
    expect(calls).to.deep.equal([['c-1', 'ignored']]);
    expect(el.shadowRoot!.querySelector('section')).to.equal(null);
  });

  // protects=account inventory entry point; fails_when=extensions require an
  // ungoverned agent to be present; no_claim=tab wiring, child API tested separately.
  it('opens Extensions even when there are no discovered candidates', async () => {
    const el = await mount([]);
    const tabs = el.shadowRoot!.querySelector('sl-tab-group')!;
    tabs.dispatchEvent(
      new CustomEvent('sl-tab-show', { detail: { name: 'extensions' } })
    );
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('extension-inventory')).to.exist;
  });

  // protects=no reload on tab switch; fails_when=leaving Extensions unmounts
  // the inventory so returning refetches it; no_claim=element identity only.
  it('keeps the inventory mounted when switching tabs', async () => {
    const el = await mount([]);
    const tabs = el.shadowRoot!.querySelector('sl-tab-group')!;
    const show = async (name: string): Promise<void> => {
      tabs.dispatchEvent(new CustomEvent('sl-tab-show', { detail: { name } }));
      await el.updateComplete;
    };
    await show('extensions');
    const first = el.shadowRoot!.querySelector('extension-inventory');
    await show('agents');
    await show('extensions');
    expect(el.shadowRoot!.querySelector('extension-inventory')).to.equal(first);
  });

  it('builds onboard commands and short labels', () => {
    expect(onboardCommandFor(candidate({ agent_kind: 'codex' }))).to.equal(
      'preloop agents onboard codex'
    );
    expect(shortWorkstation('0123456789')).to.equal('01234567');
  });
});
