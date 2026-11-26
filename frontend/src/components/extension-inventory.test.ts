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
    accessLoader?: ExtensionInventory['accessLoader'];
    updater?: ExtensionInventory['updater'];
  } = {}
): Promise<ExtensionInventory> {
  const element = await fixture<ExtensionInventory>(
    html`<extension-inventory
      .fingerprint=${options.device ? installed.fingerprint : ''}
      .loader=${options.loader ?? (async () => options.page ?? (options.device ? devicePage() : aggregatePage()))}
      .accessLoader=${options.accessLoader ?? (async () => ({ view: true, manage: options.manage ?? false }))}
      .updater=${options.updater ?? (async () => {})}
    ></extension-inventory>`
  );
  await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
  return element;
}

describe('extension inventory', () => {
  it('does not invent reported failures when device reasons are absent', async () => {
    const element = await mount({
      page: {
        items: [],
        next_cursor: null,
        completeness: 'unknown',
        coverage: [
          {
            fingerprint: 'a'.repeat(64),
            collector_version: '1.1.0',
            completeness: 'unknown',
            reasons: [],
          },
        ],
      },
    });
    const text = element.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('No specific incompleteness reason was reported');
    expect(text).not.to.contain('reported collection failures');
    expect(text).to.contain('preloop version');
  });
  // protects=AC10 only account managers get enforcement; fails_when=readers
  // or device views gain editing controls; no_claim=fixture permission loader.
  it('offers account enforcement only to managers', async () => {
    for (const options of [
      { manage: false },
      { manage: true },
      { manage: true, device: true },
    ]) {
      const element = await mount(options);
      expect(
        !!element.shadowRoot?.querySelector('extension-enforcement')
      ).to.equal(options.manage && !options.device);
    }
  });

  for (const device of [false, true]) {
    for (const manage of [false, true]) {
      it(`shows read-only constraints and non-enforcement for ${device ? 'device' : 'account'} ${manage ? 'managers' : 'viewers'}`, async () => {
        const page = device ? devicePage() : aggregatePage();
        page.items[0].version_constraint = '2.*';
        const element = await mount({ device, manage, page });
        const root = element.shadowRoot!;
        expect(root.textContent!.replace(/\s+/g, ' ')).to.contain(
          'Unapproved does not disable or block an extension.'
        );
        expect(root.textContent!.replace(/\s+/g, ' ')).to.contain(
          'Name approval is not a version-compliance verdict'
        );
        expect(
          root.querySelector('td[data-label="Approval"]')!.textContent
        ).to.contain('2.*');
        expect(root.querySelector('tbody sl-input')).to.equal(null);
      });
    }
  }

  it('does not infer an old collector from unknown coverage', async () => {
    const element = await mount({
      page: { items: [], next_cursor: null, completeness: 'unknown' },
    });
    const text = element.shadowRoot!.textContent!;
    expect(text).to.contain('unknown');
    expect(text).not.to.contain('preloop update');
  });

  it('shows project coverage and the repository scan command for a current collector', async () => {
    const element = await mount({
      page: {
        items: [],
        next_cursor: null,
        completeness: 'partial',
        coverage: [
          {
            fingerprint: 'a'.repeat(64),
            collector_version: '1.1.0-rc.1',
            completeness: 'partial',
            reasons: ['extension_project_unscanned'],
          },
        ],
      },
    });
    const text = element.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('extension_project_unscanned');
    expect(text).to.contain('preloop agents discover --report --project .');
    expect(text).to.contain('repository');
    expect(text).not.to.contain('preloop update');
  });

  it('recommends upgrading only a collector at or below the supported floor', async () => {
    for (const [version, upgrade] of [
      ['1.0.2', true],
      ['v1.0.2', true],
      ['1.0.1', true],
      ['1.0.10', false],
      ['1.1.0-rc.1', false],
      [null, false],
      ['dev', false],
    ] as const) {
      const element = await mount({
        page: {
          items: [],
          next_cursor: null,
          completeness: 'unknown',
          coverage: [
            {
              fingerprint: 'b'.repeat(64),
              collector_version: version,
              completeness: 'unknown',
              reasons: [],
            },
          ],
        },
      });
      expect(
        element.shadowRoot!.textContent!.includes('preloop update'),
        String(version)
      ).to.equal(upgrade);
    }
  });

  for (const device of [false, true]) {
    // protects=one initial fetch per view; fails_when=lifecycle hooks duplicate
    // requests; no_claim=fixture loaders count calls without a live backend.
    it(`loads access and inventory once on the initial ${device ? 'device' : 'account'} render`, async () => {
      let accessCalls = 0;
      const calls: ExtensionFilters[] = [];
      const element = await mount({
        device,
        accessLoader: async () => {
          accessCalls++;
          return { view: true, manage: false };
        },
        loader: async (filters) => {
          calls.push(filters);
          return device ? devicePage() : aggregatePage();
        },
      });
      await element.updateComplete;
      expect(accessCalls).to.equal(1);
      expect(calls.map((filters) => filters.fingerprint)).to.deep.equal([
        device ? installed.fingerprint : undefined,
      ]);

      element.fingerprint = 'd'.repeat(64);
      await waitUntil(() => calls.length >= 2);
      await element.updateComplete;
      expect(accessCalls).to.equal(2);
      expect(calls.map((filters) => filters.fingerprint)).to.deep.equal([
        device ? installed.fingerprint : undefined,
        'd'.repeat(64),
      ]);

      element.fingerprint = '';
      await waitUntil(() => calls.length >= 3);
      await element.updateComplete;
      expect(accessCalls).to.equal(3);
      expect(calls.map((filters) => filters.fingerprint)).to.deep.equal([
        device ? installed.fingerprint : undefined,
        'd'.repeat(64),
        undefined,
      ]);
    });
  }

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

  // protects=the Extensions tab downloads the policy file; fails_when=the
  // button is missing or the request is not the export; no_claim=fixture
  // loader and saver stand in for the browser download.
  it('downloads the extension policy file', async () => {
    let calls = 0;
    const saved: string[] = [];
    const element = await mount({
      device: true,
    });
    element.policyLoader = async () => {
      calls += 1;
      return '{"schema":"preloop.extension-policy.v1"}\n';
    };
    element.policySaver = (filename: string) => {
      saved.push(filename);
    };
    const button = element.shadowRoot!.querySelector(
      '[data-testid="download-policy"]'
    ) as HTMLElement;
    expect(button.textContent).to.contain('Download policy file');
    button.click();
    await waitUntil(() => calls === 1 && saved.length === 1);
    expect(saved).to.deep.equal(['preloop-extension-policy.json']);
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
