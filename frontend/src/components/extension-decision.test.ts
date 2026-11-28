import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import {
  displayName,
  extensionPurpose,
  extensionSource,
  groupBundled,
} from './extension-decision';
import type {
  ExtensionAggregate,
  ExtensionFilters,
  ExtensionPage,
} from '../extension-inventory-api';

const row = (changes: Partial<ExtensionAggregate>): ExtensionAggregate => ({
  agent_kind: 'claude_code',
  extension_kind: 'skill',
  extension_id: 'careful',
  approval_state: 'unknown',
  device_count: 1,
  versions: [],
  source_kinds: ['local'],
  ...changes,
});

const plugin = row({
  extension_kind: 'plugin',
  extension_id: 'hookify@claude-plugins-official',
  source_kinds: ['official_marketplace'],
  first_seen_at: '2026-11-20T10:00:00Z',
  people_count: 2,
  device_count: 3,
  capabilities: [
    { key: 'shell', label: 'Runs shell commands' },
    { key: 'hooks', label: 'Registers hooks on PreToolUse, Stop' },
  ],
  details: {
    description: 'Create hooks from conversation patterns',
    author: 'Example Publisher',
    marketplace: 'claude-plugins-official',
    homepage_host: 'example.com',
    license: 'Apache-2.0',
    bundles: { skills: 1, commands: 4, hooks: 4, mcp_servers: 0, agents: 1 },
    bundle_names: {
      skills: ['writing-rules'],
      commands: ['configure', 'list'],
    },
    hook_events: ['PreToolUse', 'Stop'],
  },
});
const bundled = row({
  extension_id: 'hookify:writing-rules',
  source_kinds: ['official_marketplace'],
  details: {
    description: 'Write hook rules',
    plugin: 'hookify@claude-plugins-official',
  },
});

describe('extension decision context', () => {
  // protects=a purpose line for every kind that has facts; fails_when=a row
  // prints a placeholder instead of nothing, or hooks/MCP stay unexplained.
  it('derives a one-line purpose or nothing', () => {
    expect(extensionPurpose(row({}))).to.equal('');
    expect(
      extensionPurpose(
        row({
          extension_kind: 'hook',
          extension_id: 'Stop',
          details: {
            hook_matchers: [
              {
                count: 2,
                commands: [
                  { executable: 'timeline-stop-hook', preloop_managed: false },
                  { executable: 'preloop', preloop_managed: true },
                ],
              },
            ],
          },
        })
      )
    ).to.equal('Runs timeline-stop-hook and Preloop on Stop');
    expect(
      extensionPurpose(
        row({
          extension_kind: 'mcp_server',
          details: { transport: 'http', url_host: 'example.com' },
        })
      )
    ).to.equal('Connects to example.com (HTTP)');
    expect(
      extensionPurpose(
        row({
          extension_kind: 'mcp_server',
          details: { transport: 'stdio', command: 'npx' },
        })
      )
    ).to.equal('Starts npx on the device (stdio)');
    expect(
      extensionPurpose(
        row({ extension_kind: 'plugin', source_kinds: ['builtin'] })
      )
    ).to.equal('Ships with Claude Code');
  });

  it('states the source as a trust hint with marketplace and publisher', () => {
    expect(extensionSource(plugin)).to.deep.equal({
      label: 'Official marketplace',
      detail: 'claude-plugins-official · by Example Publisher',
    });
    expect(extensionSource(row({ source_kinds: ['git'] })).label).to.equal(
      'Git URL'
    );
    expect(displayName(plugin)).to.equal('hookify');
  });

  it('nests bundled skills under their plugin only when the plugin is listed', () => {
    const { top, children } = groupBundled([plugin, bundled, row({})]);
    expect(top.map((item) => item.extension_id)).to.deep.equal([
      'hookify@claude-plugins-official',
      'careful',
    ]);
    expect(
      [...children.values()][0].map((item) => item.extension_id)
    ).to.deep.equal(['hookify:writing-rules']);
    expect(groupBundled([bundled]).top).to.have.length(1);
  });

  // protects=the decision row and filters; fails_when="No description
  // reported" returns, capabilities or people are missing, bundled skills are
  // separate rows, or the capability filter is not sent.
  it('renders the decision row, groups contents and filters by capability', async () => {
    const calls: ExtensionFilters[] = [];
    const page: ExtensionPage = {
      completeness: 'complete',
      next_cursor: null,
      items: [
        plugin,
        bundled,
        row({ capabilities: [{ key: 'unknown', label: 'Unknown' }] }),
      ],
    };
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .loader=${async (filters: ExtensionFilters) => {
          calls.push(filters);
          return page;
        }}
        .accessLoader=${async () => ({ view: true, manage: false })}
        .pinLoader=${async () => []}
        .deviceLoader=${async () => ({ items: [], next_cursor: null, completeness: 'complete' })}
      ></extension-inventory>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('tbody tr'));
    const root = element.shadowRoot!;
    const text = root.textContent ?? '';
    expect(text).not.to.contain('No description reported');
    const rows = [...root.querySelectorAll('tbody > tr')];
    expect(rows).to.have.length(2);
    const first = (rows[0].textContent ?? '').replace(/\s+/g, ' ');
    for (const fact of [
      'hookify',
      'Create hooks from conversation patterns',
      'Claude Code · Plugin',
      'Official marketplace',
      'by Example Publisher',
      'Registers hooks on PreToolUse, Stop',
      '3 devices',
      '2 people',
    ])
      expect(first).to.contain(fact);
    expect(rows[1].textContent).to.contain('Not known');
    (root.querySelector('.bundle-toggle') as HTMLElement).click();
    await element.updateComplete;
    expect(root.querySelector('.bundled-row')?.textContent).to.contain(
      'Write hook rules'
    );
    (root.querySelector('.toggle-details') as HTMLElement).click();
    await element.updateComplete;
    const facts = (
      root.querySelector('.extension-details')?.textContent ?? ''
    ).replace(/\s+/g, ' ');
    for (const fact of [
      'Apache-2.0',
      'example.com',
      'writing-rules',
      'configure, list',
    ])
      expect(facts).to.contain(fact);
    const select = root.querySelector(
      '.capability-filter'
    ) as HTMLSelectElement;
    select.value = 'network';
    select.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => calls.some((call) => call.capability === 'network'));
  });
});
