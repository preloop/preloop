import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import { fleetExtensionFindings } from '../test-helpers/fleet-extension-findings';
import {
  EXTENSION_REASONS,
  reportStale,
  type ExtensionInventory,
} from './extension-inventory';
import type {
  ExtensionPage,
  InstalledExtension,
  ExtensionFilters,
  ExtensionApprovalState,
  ExtensionCoverage,
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

const textOf = (element: ExtensionInventory): string =>
  element.shadowRoot!.textContent!.replace(/\s+/g, ' ');
const recent = (): string => new Date(Date.now() - 5 * 60000).toISOString();

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
  it('opens the exact profile and project from a finding beyond the first page', async () => {
    const row = { ...installed, content_hash: 'sha256:' + 'd'.repeat(64) };
    const finding = {
      ...fleetExtensionFindings[0],
      evidence: {
        extension_id: row.extension_id,
        kind: row.extension_kind,
        agent_kind: row.agent_kind,
        profile_ref: row.profile_ref,
        scope: row.scope,
        project_ref_hash: row.project_ref_hash,
      },
    };
    const loads: ExtensionFilters[] = [];
    const el = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .fingerprint=${row.fingerprint}
        .finding=${finding}
        .accessLoader=${async () => ({ view: true, manage: true })}
        .pinLoader=${async () => []}
        .loader=${async (filters: ExtensionFilters) => {
          loads.push({ ...filters });
          return {
            ...devicePage(),
            items: [filters.cursor ? row : { ...row, profile_ref: 'other' }],
            next_cursor: filters.cursor ? null : 'next',
          };
        }}
      ></extension-inventory>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('.extension-details'));
    const root = el.shadowRoot!;
    expect(loads.map((filter) => filter.cursor)).to.deep.equal([
      undefined,
      'next',
    ]);
    expect(loads[0].extension_id).to.equal(row.extension_id);
    expect(root.querySelectorAll('.identity')).to.have.length(1);
    expect(
      root.querySelector('.toggle-details')?.getAttribute('aria-expanded')
    ).to.equal('true');
    expect(root.querySelector('.read-content')).to.exist;
    expect(root.querySelector('.approve-exact')).to.exist;
    el.style.width = '900px';
    const table = root.querySelector<HTMLElement>('.table-scroll')!;
    expect(table.scrollWidth).to.be.at.most(table.clientWidth + 1);
    const cell = root
      .querySelector('[data-label="Approval"]')!
      .getBoundingClientRect();
    const button = root
      .querySelector('.approve-exact')!
      .getBoundingClientRect();
    expect(button.right).to.be.at.most(cell.right);
    expect(button.left).to.be.at.least(cell.left);
  });

  it('confirms the selected names once and keeps cancelled decisions untouched', async () => {
    const page = aggregatePage();
    page.items.push({ ...page.items[0], extension_id: 'second' });
    const element = await mount({ manage: true, page });
    const root = element.shadowRoot!;
    const all = root.querySelector<HTMLInputElement>('.bulk input')!;
    const calls: string[][] = [];
    element.bulkUpdater = async (rows, state) => {
      calls.push([state, ...rows.map((row) => row.extension_id)]);
    };
    let confirmed = false;
    element.confirmBulk = async (options) => {
      expect(options.message).to.contain(
        confirmed ? '1 extension' : '2 extensions'
      );
      expect(options.detail).to.contain('do not set content pins');
      return confirmed;
    };
    all.click();
    await element.updateComplete;
    root.querySelector<HTMLElement>('.bulk sl-button')!.click();
    await element.updateComplete;
    await waitUntil(
      () => !root.querySelector<HTMLInputElement>('.selection input')!.disabled
    );
    expect(calls).to.be.empty;
    root
      .querySelector<HTMLInputElement>('input[aria-label="Select second"]')!
      .click();
    await element.updateComplete;
    expect(all.indeterminate).to.equal(true);
    confirmed = true;
    root.querySelectorAll<HTMLElement>('.bulk sl-button')[1].click();
    await waitUntil(
      () => calls.length === 1 && !root.querySelector('.bulk sl-button')
    );
    expect(calls).to.deep.equal([['unapproved', 'reviewer@community']]);
  });

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
            reported_at: recent(),
          },
        ],
      },
    });
    const text = textOf(element);
    expect(text).to.contain('This device did not report a reason.');
    expect(element.shadowRoot!.querySelector('.reason')).to.equal(null);
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
        // The standing caveats live beside the Approval heading, not above
        // the table.
        const how = {
          textContent: root
            .querySelector('th.approval-heading details.how')!
            .textContent!.replace(/\s+/g, ' '),
          hasAttribute: (name: string): boolean =>
            root
              .querySelector('th.approval-heading details.how')!
              .hasAttribute(name),
        };
        expect(how.textContent).to.contain('does not disable or block');
        expect(how.textContent).to.contain('does not check versions');
        expect(how.textContent).to.contain(
          'does not show what an agent loaded'
        );
        expect(how.textContent).to.contain('whole account');
        expect(how.hasAttribute('open')).to.equal(false);
        expect(root.querySelector('.summary-text')!.textContent).not.to.contain(
          'block'
        );
        // Narrow screens hide the table heading; a copy stays reachable there.
        expect(root.querySelector('.how-narrow details.how')).to.exist;
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
    expect(textOf(element)).to.contain('Coverage unknown');
    expect(textOf(element)).not.to.contain('preloop update');
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
            reported_at: recent(),
          },
        ],
      },
    });
    const reason = element.shadowRoot!.querySelector(
      '.reason[data-code="extension_project_unscanned"]'
    )!;
    expect(reason.querySelector('.code')!.textContent).to.equal(
      'extension_project_unscanned'
    );
    expect(reason.querySelector('.command code')!.textContent).to.equal(
      'preloop agents discover --report --project .'
    );
    expect(textOf(element)).not.to.contain('preloop update');
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
              reported_at: recent(),
            },
          ],
        },
      });
      expect(
        !!element.shadowRoot!.querySelector(
          '.reason[data-code="collector_too_old"]'
        ),
        String(version)
      ).to.equal(upgrade);
      expect(
        textOf(element).includes('preloop update'),
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
      // Permissions are read once per mount, not on every refresh.
      expect(accessCalls).to.equal(1);
      expect(calls.map((filters) => filters.fingerprint)).to.deep.equal([
        device ? installed.fingerprint : undefined,
        'd'.repeat(64),
      ]);

      element.fingerprint = '';
      await waitUntil(() => calls.length >= 3);
      await element.updateComplete;
      expect(accessCalls).to.equal(1);
      expect(calls.map((filters) => filters.fingerprint)).to.deep.equal([
        device ? installed.fingerprint : undefined,
        'd'.repeat(64),
        undefined,
      ]);
    });
  }

  // protects=device facts and hash-only provenance; fails_when=columns or scope
  // are missing; no_claim=synthetic HTTP DTO, no agent execution evidence.
  it('keeps decision controls visible and moves hash provenance into Details without an approval editor for viewers', async () => {
    const element = await mount({ device: true });
    const root = element.shadowRoot!;
    expect(
      [...root.querySelectorAll('th')].map((th) =>
        th.firstChild?.textContent?.trim()
      )
    ).to.deep.equal([
      'Extension',
      'Source',
      'What it can do',
      'First seen',
      'Approval',
    ]);
    expect(root.querySelector('tbody')?.textContent).to.contain(
      'reviewer@community'
    );
    expect(root.querySelector('tbody')?.textContent).to.contain('Claude Code');
    expect(root.querySelector('tbody')?.textContent).not.to.contain('sha256:');
    element.pinLoader = async () => [];
    root.querySelector<HTMLElement>('.toggle-details')!.click();
    await element.updateComplete;
    expect(root.querySelector('.extension-details')?.textContent).to.contain(
      installed.source_ref_hash!
    );
    expect(root.querySelector('.extension-details')?.textContent).to.contain(
      installed.project_ref_hash!
    );
    expect(root.querySelector('tbody')?.textContent).to.contain('Project');
    expect(root.querySelector('tbody sl-select')).to.equal(null);
    expect(
      root.querySelector('details.how')!.textContent!.replace(/\s+/g, ' ')
    ).to.contain('does not show what an agent loaded or ran');
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
    expect(
      element.shadowRoot?.querySelector('.coverage-badge')?.textContent
    ).to.equal('Scan partial');
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

describe('extension inventory notices', () => {
  // The fixed codes the collector sends (agents_inventory_extensions_report.go).
  const collectorCodes = [
    'extension_file_limit',
    'extension_hook_unbound',
    'extension_limit',
    'extension_malformed',
    'extension_project_unscanned',
    'extension_symlink',
    'extension_unreadable',
    'extension_unsafe_path',
    'extension_unsupported_scope',
    'extension_unsupported_version',
  ];
  const device = (
    over: Partial<ExtensionCoverage> = {}
  ): ExtensionCoverage => ({
    fingerprint: 'e'.repeat(64),
    collector_version: '1.1.0-rc.3',
    completeness: 'complete',
    reasons: [],
    reported_at: recent(),
    ...over,
  });
  const withCoverage = (
    completeness: ExtensionPage['completeness'],
    coverage: ExtensionCoverage[]
  ): ExtensionPage => ({ ...aggregatePage(), completeness, coverage });

  it('maps every collector reason code to its own sentence and step', () => {
    expect(Object.keys(EXTENSION_REASONS).sort()).to.deep.equal(collectorCodes);
    const steps = new Set<string>();
    for (const code of collectorCodes) {
      const reason = EXTENSION_REASONS[code];
      expect(reason.text, code).to.match(/\.$/);
      expect(reason.text, code).not.to.contain('extension_');
      expect(reason.text + reason.step, code).not.to.contain('\u2014');
      steps.add(reason.step + (reason.command ?? ''));
    }
    expect(steps.size).to.be.greaterThan(5);
  });

  it('renders each reported code as plain text with the code in small type', async () => {
    const element = await mount({
      page: withCoverage('partial', [
        device({ completeness: 'partial', reasons: collectorCodes }),
      ]),
    });
    for (const code of collectorCodes) {
      const item = element.shadowRoot!.querySelector(
        `.reason[data-code="${code}"]`
      )!;
      expect(item, code).to.exist;
      expect(item.querySelector('p')!.textContent).to.equal(
        EXTENSION_REASONS[code].text
      );
      expect(item.querySelector('.code')!.textContent).to.equal(code);
    }
  });

  it('shows an unknown code verbatim with a support step', async () => {
    const element = await mount({
      page: withCoverage('partial', [
        device({ completeness: 'partial', reasons: ['extension_new_thing'] }),
      ]),
    });
    const item = element.shadowRoot!.querySelector(
      '.reason[data-code="extension_new_thing"]'
    )!;
    expect(item.textContent).to.contain('Contact support');
  });

  for (const [completeness, badge, variant] of [
    ['complete', 'Complete', 'success'],
    ['partial', 'Scan partial', 'warning'],
    ['unknown', 'Coverage unknown', 'neutral'],
  ] as const) {
    it(`shows a ${badge} badge with the device and collector, collapsed`, async () => {
      const element = await mount({
        page: withCoverage(completeness, [device({ completeness })]),
      });
      const root = element.shadowRoot!;
      const badgeEl = root.querySelector('.coverage-badge')!;
      expect(badgeEl.textContent).to.equal(badge);
      expect(badgeEl.getAttribute('variant')).to.equal(variant);
      expect(
        root
          .querySelector('.coverage summary')!
          .textContent!.replace(/\s+/g, ' ')
      ).to.contain('Device eeeeeeee · Collector 1.1.0-rc.3');
      expect(root.querySelector('details.coverage')!.hasAttribute('open')).to.be
        .false;
      // The table is the first thing after the summary and filters.
      expect(root.querySelector('table')).to.exist;
    });
  }

  it('summarizes counts by kind, devices and last report time', async () => {
    const page = withCoverage('complete', [device()]);
    page.items = [
      ...page.items,
      { ...page.items[0], extension_kind: 'skill', extension_id: 'a' },
      { ...page.items[0], extension_kind: 'skill', extension_id: 'b' },
    ];
    const element = await mount({ page });
    const text = element
      .shadowRoot!.querySelector('.summary-text')!
      .textContent!.replace(/\s+/g, ' ');
    expect(text).to.contain('3 extensions (2 skills, 1 plugin)');
    expect(text).to.contain('1 device reporting');
    expect(text).to.contain('Last report 5m ago');
  });

  it('does not tell a fresh, current device to sign in or report', async () => {
    const element = await mount({
      page: withCoverage('partial', [
        device({
          completeness: 'partial',
          reasons: ['extension_hook_unbound', 'extension_malformed'],
        }),
      ]),
    });
    const text = textOf(element);
    expect(text).not.to.contain('preloop auth login');
    expect(text).not.to.contain('Reload this page');
    expect(text).not.to.contain('preloop update');
  });

  it('does not label an older server with an omitted report time as stale', async () => {
    const legacy = device();
    delete legacy.reported_at;
    const element = await mount({ page: withCoverage('complete', [legacy]) });
    expect(textOf(element)).not.to.contain('has not reported in the last day');
    expect(element.shadowRoot!.querySelector('.device .command')).to.equal(
      null
    );
  });

  it('labels account-wide coverage and offers report steps for an empty agent kind', async () => {
    const element = await mount({
      page: { ...withCoverage('complete', [device()]), items: [] },
    });
    element.agentKind = 'claude_code';
    element.emptyText =
      'No device has reported extensions for claude_code agents.';
    await element.updateComplete;
    await element.refresh();
    await element.updateComplete;
    expect(textOf(element)).to.contain('Account coverage (all agent kinds)');
    expect(textOf(element)).to.contain(
      '1 device reporting across all agent kinds'
    );
    expect(textOf(element)).to.contain(
      'No device has reported extensions for claude_code agents.'
    );
    expect(textOf(element)).to.contain(
      'preloop agents discover --report --project .'
    );
  });

  for (const [name, over] of [
    [
      'stale',
      { reported_at: new Date(Date.now() - 9 * 86400000).toISOString() },
    ],
  ] as const) {
    it(`tells a ${name} device to sign in and report, one command per line`, async () => {
      const element = await mount({
        page: withCoverage('complete', [device(over)]),
      });
      const commands = [
        ...element.shadowRoot!.querySelectorAll('.device .command code'),
      ].map((code) => code.textContent);
      expect(commands).to.deep.equal([
        `preloop auth login --url ${window.location.origin}`,
        'preloop agents discover --report --project .',
      ]);
    });
  }

  it('allows daily jitter and multi-day schedules and treats missing time as unknown', () => {
    const now = Date.now();
    for (const days of [1.1, 3, 7]) {
      expect(
        reportStale(
          device({
            reported_at: new Date(now - days * 86400000).toISOString(),
          }),
          now
        )
      ).to.equal(false);
    }
    expect(reportStale(device({ reported_at: null }), now)).to.equal(false);
    expect(
      reportStale(
        device({ reported_at: new Date(now - 9 * 86400000).toISOString() }),
        now
      )
    ).to.equal(true);
  });

  it('keeps the caveats visible at every width when there are no rows', async () => {
    const element = await mount({
      page: {
        items: [],
        next_cursor: null,
        completeness: 'unknown',
        coverage: [],
      },
    });
    const how = element.shadowRoot!.querySelector('.how-always details.how');
    expect(how).to.exist;
    expect(getComputedStyle(how!.parentElement!).display).not.to.equal('none');
  });

  it('shows the sign-in and report steps in the empty state', async () => {
    const element = await mount({
      page: {
        items: [],
        next_cursor: null,
        completeness: 'unknown',
        coverage: [],
      },
    });
    const empty = element.shadowRoot!.querySelector('.empty-state')!;
    expect(empty.textContent).to.contain('No extensions reported yet.');
    expect(
      [...empty.querySelectorAll('.command code')].map((c) => c.textContent)
    ).to.deep.equal([
      `preloop auth login --url ${window.location.origin}`,
      'preloop agents discover --report --project .',
    ]);
  });

  it('keeps commands out of sentences and copies them', async () => {
    const copied: string[] = [];
    const element = await mount({
      page: withCoverage('partial', [
        device({ completeness: 'partial', reasons: ['extension_unreadable'] }),
      ]),
    });
    element.copier = async (text) => {
      copied.push(text);
    };
    const root = element.shadowRoot!;
    for (const p of root.querySelectorAll('p'))
      expect(p.querySelector('code'), p.textContent!).to.equal(null);
    (root.querySelector('.command sl-button.copy') as HTMLElement).click();
    await waitUntil(() => copied.length === 1);
    expect(copied).to.deep.equal(['preloop agents extensions --project .']);
  });

  it('explains Unapproved where it is set, with a link to Enforcement', async () => {
    const page = aggregatePage();
    const element = await mount({
      manage: true,
      page,
      updater: async (_row, state) => {
        page.items[0].approval_state = state;
      },
    });
    const root = element.shadowRoot!;
    expect(root.querySelector('.unapproved-hint')).to.equal(null);
    const select = root.querySelector('tbody sl-select') as HTMLElement & {
      value: string;
    };
    select.value = 'unapproved';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => root.querySelector('.unapproved-hint'));
    const hint = root.querySelector('.unapproved-hint')!;
    expect(hint.textContent).to.contain('does not disable or block');
    expect(hint.querySelector('a')!.getAttribute('href')).to.equal(
      '#extension-enforcement'
    );
    expect(root.querySelector('#extension-enforcement')).to.exist;
  });

  it('shows a table skeleton while the first page loads', async () => {
    let release!: (page: ExtensionPage) => void;
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .loader=${() => new Promise<ExtensionPage>((r) => (release = r))}
        .accessLoader=${async () => ({ view: true, manage: false })}
      ></extension-inventory>`
    );
    await waitUntil(() => element.shadowRoot!.querySelector('.skeleton'));
    expect(element.shadowRoot!.querySelector('table')).to.equal(null);
    release(aggregatePage());
    await waitUntil(() => element.shadowRoot!.querySelector('table'));
    expect(element.shadowRoot!.querySelector('.skeleton')).to.equal(null);
  });

  it('keeps rows visible while an approval refresh loads', async () => {
    const page = aggregatePage();
    let hold: Promise<void> | null = null;
    let release!: () => void;
    const element = await mount({
      manage: true,
      loader: async () => {
        if (hold) await hold;
        return page;
      },
      updater: async () => {
        hold = new Promise((r) => (release = r));
      },
    });
    const select = element.shadowRoot!.querySelector(
      'tbody sl-select'
    ) as HTMLElement & { value: string };
    select.value = 'approved';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await waitUntil(() => hold !== null);
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('table')).to.exist;
    expect(element.shadowRoot!.querySelector('.skeleton')).to.equal(null);
    release();
  });
});
