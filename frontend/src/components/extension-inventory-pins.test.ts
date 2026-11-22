import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import type {
  ExtensionAggregate,
  ExtensionFilters,
  ExtensionKey,
  ExtensionPage,
  ExtensionPin,
  InstalledExtension,
} from '../extension-inventory-api';

const H1 = 'sha256:' + '1'.repeat(64);
const H2 = 'sha256:' + '2'.repeat(64);

const device = (
  effective_state: InstalledExtension['effective_state'],
  content_hash: string | null
): InstalledExtension => ({
  fingerprint: 'a'.repeat(64),
  profile_ref: 'current',
  agent_kind: 'claude_code',
  extension_kind: 'skill',
  extension_id: 'review',
  id_hashed: false,
  version: null,
  source_kind: 'local',
  source_ref_hash: null,
  scope: 'user',
  project_ref_hash: null,
  enabled: 'unknown',
  present: true,
  first_seen_at: new Date().toISOString(),
  last_seen_at: new Date().toISOString(),
  approval_state: 'approved',
  effective_state,
  content_hash,
  content_hash_status: content_hash ? 'ok' : 'too_large',
});

const aggregate: ExtensionAggregate = {
  agent_kind: 'claude_code',
  extension_kind: 'skill',
  extension_id: 'review',
  device_count: 3,
  versions: [],
  source_kinds: ['local'],
  approval_state: 'approved',
  pin_count: 1,
  content_hashes: [
    { content_hash: H1, status: 'ok', device_count: 2, pinned: true },
    { content_hash: H2, status: 'ok', device_count: 1, pinned: false },
    { content_hash: null, status: 'too_large', device_count: 1, pinned: false },
  ],
};
const pin: ExtensionPin = {
  id: 'pin-1',
  content_hash: H1,
  version: null,
  note: null,
  pinned_by_user_id: 'user-1',
  pinned_by: 'jane.doe',
  pinned_at: new Date().toISOString(),
};

interface Calls {
  updates: [ExtensionKey, string, string | undefined][];
  added: string[];
  removed: string[];
  filters: ExtensionFilters[];
}

async function mount(options: {
  page: ExtensionPage;
  manage: boolean;
  fingerprint?: string;
  confirm?: boolean;
  pins?: ExtensionPin[];
}): Promise<[ExtensionInventory, Calls]> {
  const calls: Calls = { updates: [], added: [], removed: [], filters: [] };
  const element = await fixture<ExtensionInventory>(
    html`<extension-inventory
      .fingerprint=${options.fingerprint ?? ''}
      .loader=${async (filters: ExtensionFilters) => {
        calls.filters.push(filters);
        return options.page;
      }}
      .accessLoader=${async () => ({ view: true, manage: options.manage })}
      .updater=${async (row: ExtensionKey, state: string, hash?: string) => {
        calls.updates.push([row, state, hash]);
      }}
      .pinLoader=${async () => options.pins ?? [pin]}
      .pinAdder=${async (_row: ExtensionKey, hash: string) => {
        calls.added.push(hash);
        return pin;
      }}
      .pinRemover=${async (_row: ExtensionKey, id: string) => {
        calls.removed.push(id);
      }}
      .confirmUnpin=${() => options.confirm ?? true}
    ></extension-inventory>`
  );
  await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
  return [element, calls];
}

const page = (items: ExtensionPage['items']): ExtensionPage => ({
  items,
  next_cursor: null,
  completeness: 'complete',
});

describe('extension inventory content pins', () => {
  // protects=device badges and one-request exact approval; fails_when=a
  // changed or unverified row reads as plain approved, or the exact-content
  // action drops the hash or appears for viewers; no_claim=fixture loaders
  // stand in for the API.
  it('badges changed and unverified devices and approves exact content', async () => {
    const items = [
      device('content_changed', H2),
      device('content_unverified', null),
    ];
    const [element, calls] = await mount({
      page: page(items),
      manage: true,
      fingerprint: 'a'.repeat(64),
    });
    const root = element.shadowRoot!;
    expect(root.querySelector('.content-changed')?.textContent).to.equal(
      'Content changed'
    );
    expect(root.querySelector('.content-unverified')).to.exist;
    // No hash, nothing exact to approve: only the changed row offers it.
    const buttons = root.querySelectorAll<HTMLElement>('.approve-exact');
    expect(buttons.length).to.equal(1);
    buttons[0].click();
    await waitUntil(() => calls.updates.length === 1);
    expect(calls.updates[0][1]).to.equal('approved');
    expect(calls.updates[0][2]).to.equal(H2);

    const [viewer] = await mount({
      page: page(items),
      manage: false,
      fingerprint: 'a'.repeat(64),
    });
    expect(viewer.shadowRoot!.querySelector('.approve-exact')).to.be.null;
    expect(viewer.shadowRoot!.querySelector('.content-changed')).to.exist;
  });

  // protects=hash list with counts and pin attribution, plus Pin action;
  // fails_when=the list hides a bucket, the pinned row lacks who/when, or a
  // viewer can pin; no_claim=fixture pins stand in for GET approval.
  it('lists observed hashes and pins one', async () => {
    const [element, calls] = await mount({
      page: page([aggregate]),
      manage: true,
    });
    const root = element.shadowRoot!;
    expect(root.querySelector('.pinned')?.textContent).to.equal('Pinned (1)');
    expect(root.querySelector('.content-changed')).to.exist;
    root.querySelector<HTMLElement>('.toggle-hashes')!.click();
    await waitUntil(() => root.querySelectorAll('.hashes li').length === 3);
    await waitUntil(() =>
      root.querySelector('.hashes')!.textContent!.includes('jane.doe')
    );
    const text = root.querySelector('.hashes')!.textContent!;
    expect(text).to.include('2 devices');
    expect(text).to.include('No hash (Too large)');
    const pinButtons = root.querySelectorAll<HTMLElement>('.pin');
    expect(pinButtons.length).to.equal(1);
    pinButtons[0].click();
    await waitUntil(() => calls.added.length === 1);
    expect(calls.added).to.deep.equal([H2]);

    const [viewer] = await mount({ page: page([aggregate]), manage: false });
    viewer.shadowRoot!.querySelector<HTMLElement>('.toggle-hashes')!.click();
    await waitUntil(
      () => viewer.shadowRoot!.querySelectorAll('.hashes li').length === 3
    );
    expect(viewer.shadowRoot!.querySelector('.pin, .unpin')).to.be.null;
  });

  // protects=unpin is confirmed; fails_when=cancel still deletes or confirm
  // does not; no_claim=confirmUnpin stub stands in for the dialog.
  it('unpins only after confirmation', async () => {
    for (const confirm of [false, true]) {
      const [element, calls] = await mount({
        page: page([aggregate]),
        manage: true,
        confirm,
      });
      const root = element.shadowRoot!;
      root.querySelector<HTMLElement>('.toggle-hashes')!.click();
      await waitUntil(() => !!root.querySelector('.unpin'));
      root.querySelector<HTMLElement>('.unpin')!.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(calls.removed).to.deep.equal(confirm ? ['pin-1'] : []);
    }
  });

  // protects=filters for the new states; fails_when=content_changed cannot be
  // selected or is not sent; no_claim=none beyond protects.
  it('filters by the content states', async () => {
    const [element, calls] = await mount({
      page: page([aggregate]),
      manage: false,
    });
    const select = element.shadowRoot!.querySelector('sl-select')!;
    const values = [...select.querySelectorAll('sl-option')].map((o) =>
      o.getAttribute('value')
    );
    expect(values).to.include.members([
      'content_changed',
      'content_unverified',
    ]);
    (select as unknown as { value: string }).value = 'content_changed';
    select.dispatchEvent(new Event('sl-change'));
    await waitUntil(() => calls.filters.length === 2);
    expect(calls.filters[1].state).to.equal('content_changed');
  });
});
