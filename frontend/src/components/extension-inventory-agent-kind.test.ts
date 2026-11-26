import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import type {
  ExtensionAggregate,
  ExtensionFilters,
  ExtensionPage,
} from '../extension-inventory-api';

const row = (agent_kind: string, extension_id: string): ExtensionAggregate => ({
  agent_kind,
  extension_kind: 'skill',
  extension_id,
  device_count: 1,
  versions: ['1.0.0'],
  source_kinds: ['user_local'],
  approval_state: 'unknown',
});
const account = [row('claude_code', 'reviewer'), row('codex', 'linter')];

async function mount(
  options: {
    kind?: string;
    manage?: boolean;
    items?: ExtensionAggregate[];
  } = {}
): Promise<{ element: ExtensionInventory; filters: ExtensionFilters[] }> {
  const filters: ExtensionFilters[] = [];
  const items = options.items ?? account;
  const loader = async (f: ExtensionFilters): Promise<ExtensionPage> => {
    filters.push(f);
    return {
      items: items.filter(
        (r) =>
          (!f.agent_kind || r.agent_kind === f.agent_kind) &&
          (!f.state || r.approval_state === f.state) &&
          (!f.q || r.extension_id.includes(f.q))
      ),
      next_cursor: null,
      completeness: 'complete',
    };
  };
  const element = await fixture<ExtensionInventory>(
    html`<extension-inventory
      .agentKind=${options.kind ?? 'claude_code'}
      emptyText="No device has reported extensions for claude_code agents."
      .loader=${loader}
      .accessLoader=${async () => ({ view: true, manage: options.manage ?? false })}
      .pinLoader=${async () => []}
    ></extension-inventory>`
  );
  await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
  return { element, filters };
}

describe('extension inventory scoped to an agent kind', () => {
  it('requests and renders only rows for the agent kind', async () => {
    const { element, filters } = await mount();
    expect(filters[0].agent_kind).to.equal('claude_code');
    const text = element.shadowRoot!.textContent ?? '';
    expect(text).to.contain('reviewer');
    expect(text).not.to.contain('linter');
  });

  it('reports the loaded count to the page', async () => {
    const counts: number[] = [];
    const filters: ExtensionFilters[] = [];
    await fixture(
      html`<div
        @extensions-loaded=${(e: CustomEvent<{ count: number }>) =>
          counts.push(e.detail.count)}
      >
        <extension-inventory
          .agentKind=${'codex'}
          .loader=${async (f: ExtensionFilters) => {
            filters.push(f);
            return {
              items: [account[1]],
              next_cursor: null,
              completeness: 'complete',
            };
          }}
          .accessLoader=${async () => ({ view: true, manage: false })}
        ></extension-inventory>
      </div>`
    );
    await waitUntil(() => counts.length > 0);
    expect(counts[0]).to.equal(1);
    expect(filters[0].agent_kind).to.equal('codex');
  });

  it('puts the report commands on their own lines under a custom empty text', async () => {
    const { element } = await mount({ items: [] });
    const commands = [
      ...element.shadowRoot!.querySelectorAll('.command code'),
    ].map((code) => code.textContent);
    expect(commands).to.include('preloop agents discover --report --project .');
    expect(
      element.shadowRoot!.querySelector('.empty')!.textContent
    ).not.to.contain('preloop');
  });

  it('says a filter emptied the table instead of the custom empty text', async () => {
    const { element } = await mount();
    const select = element.shadowRoot!.querySelector(
      'sl-select[label="Approval state"]'
    ) as HTMLElement & { value: string };
    select.value = 'approved';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => element.shadowRoot!.querySelector('.empty'));
    const text = element.shadowRoot!.querySelector('.empty')!.textContent!;
    expect(text).to.contain('No extensions reported for this selection.');
    expect(text).not.to.contain('No device has reported');
  });

  it('shows the given empty state when nothing was reported', async () => {
    const { element } = await mount({ items: [] });
    expect(element.shadowRoot!.querySelector('.empty')!.textContent).to.contain(
      'No device has reported extensions for claude_code agents.'
    );
  });

  it('distinguishes an empty search from an inventory with no reports', async () => {
    const { element } = await mount();
    const input = element.shadowRoot!.querySelector(
      'input[type="search"]'
    ) as HTMLInputElement;
    input.value = 'missing description';
    input.dispatchEvent(new Event('input'));
    await waitUntil(() => element.shadowRoot!.querySelector('.empty'));
    const text = element.shadowRoot!.querySelector('.empty')!.textContent!;
    expect(text).to.contain('No extensions reported for this selection.');
    expect(text).not.to.contain('No device has reported');
  });

  it('hides approval controls without the manage permission', async () => {
    const viewer = await mount({ manage: false });
    expect(
      viewer.element.shadowRoot!.querySelector('tbody sl-select')
    ).to.equal(null);
    const manager = await mount({ manage: true });
    expect(
      manager.element.shadowRoot!.querySelector('tbody sl-select')
    ).not.to.equal(null);
    // Account-wide enforcement stays on the account inventory.
    expect(
      manager.element.shadowRoot!.querySelector('extension-enforcement')
    ).to.equal(null);
  });
});
