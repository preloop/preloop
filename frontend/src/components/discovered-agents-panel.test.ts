import { expect, fixture, html, aTimeout } from '@open-wc/testing';

import './discovered-agents-panel';
import {
  onboardCommandFor,
  shortWorkstation,
  type DiscoveredAgentsPanel,
} from './discovered-agents-panel';
import type { DiscoveredAgentCandidate } from '../api';

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
  updater?: DiscoveredAgentsPanel['updater']
): Promise<DiscoveredAgentsPanel> {
  const el = await fixture<DiscoveredAgentsPanel>(
    html`<discovered-agents-panel
      .loader=${async () => rows}
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

  it('builds onboard commands and short labels', () => {
    expect(onboardCommandFor(candidate({ agent_kind: 'codex' }))).to.equal(
      'preloop agents onboard codex'
    );
    expect(shortWorkstation('0123456789')).to.equal('01234567');
  });
});
