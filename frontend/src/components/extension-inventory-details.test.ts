import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import type { ExtensionPage } from '../extension-inventory-api';

const page: ExtensionPage = {
  completeness: 'complete',
  next_cursor: null,
  items: [
    {
      agent_kind: 'claude_code',
      extension_kind: 'skill',
      extension_id: 'careful',
      approval_state: 'unknown',
      device_count: 2,
      versions: [],
      source_kinds: ['local'],
      details: {
        name: 'careful',
        description: 'Review changes before acting.',
        invocation: '/careful',
        script_count: 2,
      },
    },
    {
      agent_kind: 'claude_code',
      extension_kind: 'hook',
      extension_id: 'PreToolUse',
      approval_state: 'unknown',
      device_count: 1,
      versions: [],
      source_kinds: ['local'],
      details: {
        invocation: 'Runs on PreToolUse',
        hook_matchers: [
          {
            matcher: 'Bash|Edit',
            count: 1,
            commands: [{ executable: 'preloop', preloop_managed: true }],
          },
        ],
      },
    },
    {
      agent_kind: 'claude_code',
      extension_kind: 'plugin',
      extension_id: 'reviewer@community',
      approval_state: 'unknown',
      device_count: 1,
      versions: [],
      source_kinds: ['third_party_marketplace'],
      details: {
        description: 'Review helpers',
        author: 'Jane Doe',
        bundles: {
          skills: 1,
          commands: 2,
          hooks: 3,
          mcp_servers: 1,
          agents: 0,
        },
      },
    },
  ],
};

describe('extension descriptive metadata', () => {
  it('uses the CLI agent name in a Codex inspection command', async () => {
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .loader=${async () => ({
          ...page,
          items: [{ ...page.items[0], agent_kind: 'codex' }],
        })}
        .accessLoader=${async () => ({ view: true, manage: false })}
        .pinLoader=${async () => []}
      ></extension-inventory>`
    );
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('.toggle-details')
    );
    (
      element.shadowRoot!.querySelector('.toggle-details') as HTMLElement
    ).click();
    await element.updateComplete;
    expect(element.shadowRoot!.textContent).to.contain("--agent 'codex-cli'");
  });
  it('renders descriptions, invocation, sorted kinds and expandable local content commands', async () => {
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .loader=${async () => page}
        .accessLoader=${async () => ({ view: true, manage: false })}
        .pinLoader=${async () => []}
      ></extension-inventory>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('table'));
    const root = element.shadowRoot!;
    expect(root.textContent).to.contain('Review changes before acting.');
    expect(root.textContent).to.contain('/careful');
    const rows = [...root.querySelectorAll('tbody > tr')];
    expect(rows[0].textContent).to.contain('PreToolUse');
    (rows[0].querySelector('.toggle-details') as HTMLElement).click();
    await element.updateComplete;
    expect(root.textContent).to.contain('Bash|Edit');
    expect(root.textContent).to.contain('Preloop-managed');
    expect(root.textContent).to.contain(
      "preloop agents extensions show hook 'PreToolUse'"
    );
    expect(root.querySelector('.copy-command')).to.exist;
  });
  it('sends text search to the API and displays withheld descriptions explicitly', async () => {
    const queries: unknown[] = [];
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .loader=${async (filters: unknown) => {
          queries.push(filters);
          return {
            ...page,
            items: [
              { ...page.items[0], details: { description_withheld: true } },
            ],
          };
        }}
        .accessLoader=${async () => ({ view: true, manage: false })}
      ></extension-inventory>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('table'));
    expect(element.shadowRoot!.textContent).to.contain('Description withheld');
    const input = element.shadowRoot!.querySelector(
      'input[type="search"]'
    ) as HTMLInputElement;
    input.value = 'review';
    input.dispatchEvent(new Event('input'));
    await waitUntil(() => queries.length === 2);
    expect(queries[1]).to.have.property('q', 'review');
  });
});
