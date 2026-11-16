import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import type {
  ExtensionPage,
  InstalledExtension,
  ExtensionFilters,
  ExtensionApprovalState,
} from '../extension-inventory-api';

const installed: InstalledExtension = {
  fingerprint: 'a'.repeat(64),
  profile_ref: 'current',
  agent_kind: 'claude_code',
  extension_kind: 'plugin',
  extension_id: 'reviewer@community',
  id_hashed: false,
  version: '1.4.0',
  source_kind: 'third_party_marketplace',
  source_ref_hash: 'sha256:' + 'b'.repeat(64),
  scope: 'project',
  project_ref_hash: 'sha256:' + 'c'.repeat(64),
  enabled: 'false',
  present: true,
  first_seen_at: new Date(Date.now() - 3600000).toISOString(),
  last_seen_at: new Date().toISOString(),
  approval_state: 'unknown',
};
const devicePage = (): ExtensionPage => ({
  items: [{ ...installed }],
  next_cursor: null,
  completeness: 'complete',
});
const aggregatePage = (): ExtensionPage => ({
  items: [
    {
      agent_kind: 'claude_code',
      extension_kind: 'plugin',
      extension_id: 'reviewer@community',
      device_count: 3,
      versions: ['1.4.0', '1.5.0'],
      source_kinds: ['third_party_marketplace'],
      approval_state: 'unknown',
    },
  ],
  next_cursor: null,
  completeness: 'complete',
});

async function mount(
  options: {
    device?: boolean;
    manage?: boolean;
    page?: ExtensionPage;
    loader?: (filters: ExtensionFilters) => Promise<ExtensionPage>;
    updater?: ExtensionInventory['updater'];
  } = {}
): Promise<ExtensionInventory> {
  const element = await fixture<ExtensionInventory>(
    html`<extension-inventory
      .fingerprint=${options.device ? installed.fingerprint : ''}
      .loader=${options.loader ?? (async () => options.page ?? (options.device ? devicePage() : aggregatePage()))}
      .accessLoader=${async () => ({ view: true, manage: options.manage ?? false })}
      .updater=${options.updater ?? (async () => {})}
    ></extension-inventory>`
  );
  await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
  return element;
}

describe('extension inventory', () => {
  // protects=device facts and hash-only provenance; fails_when=columns or scope
  // are missing; no_claim=synthetic HTTP DTO, no agent execution evidence.
  it('renders device columns and hash prefixes without an approval editor for viewers', async () => {
    const element = await mount({ device: true });
    const root = element.shadowRoot!;
    expect(
      [...root.querySelectorAll('th')].map((th) => th.textContent)
    ).to.deep.equal([
      'Agent',
      'Kind',
      'Extension',
      'Version',
      'Source',
      'Scope',
      'Enabled',
      'First seen',
      'Approval',
    ]);
    expect(root.querySelector('tbody')?.textContent).to.contain(
      'reviewer@community'
    );
    expect(root.querySelector('tbody')?.textContent).to.contain(
      'sha256:bbbbbbbbbbbb…'
    );
    expect(root.querySelector('tbody')?.textContent).to.contain('Project');
    expect(root.querySelector('tbody sl-select')).to.equal(null);
    expect(root.textContent).to.contain('do not prove an agent loaded or ran');
  });

  // protects=account decisions; fails_when=select does not persist and reload;
  // no_claim=fixture updater stands in for the HTTP approval endpoint.
  it('renders account aggregates and persists a manager decision', async () => {
    const page = aggregatePage();
    const calls: ExtensionApprovalState[] = [];
    const element = await mount({
      manage: true,
      page,
      updater: async (_row, state) => {
        calls.push(state);
        page.items[0].approval_state = state;
      },
    });
    expect(element.shadowRoot?.textContent).to.contain('1.4.0, 1.5.0');
    const select = element.shadowRoot!.querySelector(
      'tbody sl-select'
    ) as HTMLElement & { value: string };
    select.value = 'unapproved';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(
      () =>
        (
          element.shadowRoot?.querySelector(
            'tbody sl-select'
          ) as HTMLElement & { value: string }
        )?.value === 'unapproved'
    );
    expect(calls).to.deep.equal(['unapproved']);
  });

  // protects=explicit incomplete/empty states; fails_when=empty partial scan looks
  // complete; no_claim=fixture coverage classification from the server.
  it('keeps partial and empty collection visible', async () => {
    const element = await mount({
      page: { items: [], next_cursor: null, completeness: 'partial' },
    });
    expect(element.shadowRoot?.textContent).to.contain(
      'Collection is incomplete'
    );
    expect(element.shadowRoot?.textContent).to.contain(
      'No extensions reported'
    );
    expect(element.shadowRoot?.querySelector('table')).to.equal(null);
  });

  // protects=server filters and cursor traversal; fails_when=filters are local or
  // another page replaces earlier rows; no_claim=synthetic paginated API.
  it('sends filters and appends the next cursor page', async () => {
    const calls: ExtensionFilters[] = [];
    const element = await mount({
      loader: async (filters) => {
        calls.push(filters);
        const page = aggregatePage();
        page.next_cursor = filters.cursor ? null : 'page-two';
        if (filters.cursor) page.items[0].extension_id = 'another@community';
        return page;
      },
    });
    (
      element.shadowRoot!.querySelector('.footer sl-button') as HTMLElement
    ).click();
    await waitUntil(
      () => element.shadowRoot!.querySelectorAll('tbody tr').length === 2
    );
    expect(calls[1].cursor).to.equal('page-two');
    const state = element.shadowRoot!.querySelector(
      'sl-select[label="Approval state"]'
    ) as HTMLElement & { value: string };
    state.value = 'unknown';
    state.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => calls.length === 3);
    expect(calls[2].state).to.equal('unknown');
    expect(calls[2].cursor).to.equal(undefined);
  });

  // protects=saved decisions on failure; fails_when=failed PUT looks successful;
  // no_claim=synthetic rejected request, not network availability.
  it('keeps the prior state and shows a failed approval', async () => {
    const element = await mount({
      manage: true,
      updater: async () => {
        throw new Error('Could not save approval');
      },
    });
    const select = element.shadowRoot!.querySelector(
      'tbody sl-select'
    ) as HTMLElement & { value: string };
    select.value = 'approved';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(
      () => !!element.shadowRoot?.querySelector('[role="alert"]')
    );
    expect(select.value).to.equal('unknown');
  });
});
