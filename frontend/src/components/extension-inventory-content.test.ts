import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-inventory';
import type { ExtensionInventory } from './extension-inventory';
import type {
  ExtensionAggregate,
  InstalledExtension,
  ExtensionKey,
  ExtensionPage,
  ExtensionPin,
} from '../extension-inventory-api';
import type {
  ExtensionContent,
  ExtensionContentSettings,
} from '../extension-content-api';

const H1 = 'sha256:' + '1'.repeat(64);
const H2 = 'sha256:' + '2'.repeat(64);

const row = (
  approval_state: ExtensionAggregate['approval_state']
): ExtensionAggregate => ({
  agent_kind: 'claude_code',
  extension_kind: 'skill',
  extension_id: 'deploy',
  device_count: 2,
  versions: [],
  source_kinds: ['local'],
  approval_state,
  effective_state:
    approval_state === 'approved' ? 'content_changed' : 'unknown',
  pin_count: approval_state === 'approved' ? 1 : 0,
  content_hashes: [
    { content_hash: H2, status: 'ok', device_count: 2, pinned: false },
    { content_hash: H1, status: 'ok', device_count: 0, pinned: true },
  ],
});

const pin: ExtensionPin = {
  id: 'pin-1',
  content_hash: H1,
  version: null,
  note: null,
  pinned_by_user_id: 'user-1',
  pinned_by: 'jane.doe',
  pinned_at: new Date().toISOString(),
};

const stored = (content_hash: string, body: string): ExtensionContent => ({
  extension_kind: 'skill',
  content_hash,
  files: [
    {
      path: 'SKILL.md',
      sha256: content_hash.slice(7),
      size: body.length,
      status: 'stored',
      text: body,
      redactions: {},
      verified: true,
    },
  ],
  hooks: [],
  withheld: null,
  verification: 'verified',
  redacted: false,
  redaction_count: 0,
  withheld_count: 0,
  stored_bytes: body.length,
  redaction_rules: 'gitleaks-8.30.1+preloop-1',
  collector_version: '1.2.0',
  first_stored_at: new Date().toISOString(),
  last_reported_at: new Date().toISOString(),
  retention_days: 90,
});

const settings = (
  change: Partial<ExtensionContentSettings> = {}
): ExtensionContentSettings => ({
  collect_enabled: true,
  notice_pending: true,
  stored_contents: 0,
  stored_bytes: 0,
  retention_days: 90,
  updated_by_user_id: null,
  updated_at: null,
  ...change,
});

interface Calls {
  reads: string[];
  updates: [string, string | undefined][];
  pinned: string[];
  acknowledged: number;
}

async function mount(options: {
  approval: ExtensionAggregate['approval_state'];
  kind?: ExtensionKey['extension_kind'];
  manage?: boolean;
  admin?: boolean;
  settings?: ExtensionContentSettings;
  fingerprint?: string;
}): Promise<[ExtensionInventory, Calls]> {
  const calls: Calls = { reads: [], updates: [], pinned: [], acknowledged: 0 };
  const page: ExtensionPage = {
    items: [
      { ...row(options.approval), extension_kind: options.kind ?? 'skill' },
    ],
    next_cursor: null,
    completeness: 'complete',
  };
  const element = await fixture<ExtensionInventory>(
    html`<extension-inventory
      .loader=${async () => page}
      .accessLoader=${async () => ({
        view: true,
        manage: options.manage ?? true,
      })}
      .updater=${async (_row: ExtensionKey, state: string, hash?: string) => {
        calls.updates.push([state, hash]);
      }}
      .pinLoader=${async () => (options.approval === 'approved' ? [pin] : [])}
      .pinAdder=${async (_row: ExtensionKey, hash: string) => {
        calls.pinned.push(hash);
        return pin;
      }}
      .contentLoader=${async (_kind: string, hash: string) => {
        calls.reads.push(hash);
        return stored(
          hash,
          hash === H1 ? '# Deploy\n\nAsk first.\n' : '# Deploy\n\nNever ask.\n'
        );
      }}
      .contentSettingsLoader=${async () => options.settings ?? settings()}
      .contentSettingsSaver=${async () => {
        calls.acknowledged++;
        return settings({ notice_pending: false });
      }}
      .contentAccessLoader=${async () => options.admin ?? true}
      .deviceLoader=${async () => ({
        items: [],
        next_cursor: null,
        completeness: 'complete',
      })}
    ></extension-inventory>`
  );
  await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
  await element.updateComplete;
  return [element, calls];
}

async function expand(element: ExtensionInventory): Promise<void> {
  element.shadowRoot!.querySelector<HTMLElement>('.toggle-hashes')!.click();
  await waitUntil(() => element.shadowRoot!.querySelector('.hashes'));
  await element.updateComplete;
}

async function read(
  element: ExtensionInventory,
  index: number
): Promise<HTMLElement> {
  const buttons =
    element.shadowRoot!.querySelectorAll<HTMLElement>('.read-content');
  buttons[index].click();
  await element.updateComplete;
  const viewer = element.shadowRoot!.querySelector<HTMLElement>(
    'extension-content-viewer'
  )!;
  await waitUntil(() => viewer.shadowRoot?.querySelector('.rendered'));
  return viewer;
}

describe('extension inventory content', () => {
  it('explains that MCP bodies are not collected and offers no content read', async () => {
    const [element, calls] = await mount({
      approval: 'approved',
      kind: 'mcp_server',
    });
    await expand(element);
    expect(element.shadowRoot!.querySelector('.read-content')).to.equal(null);
    expect(element.shadowRoot!.textContent).to.contain(
      'MCP server bodies are not collected'
    );
    expect(calls.reads).to.deep.equal([]);
  });

  // protects=content is read only on an explicit click, for the row's hash;
  // fails_when=expanding a row reads content (an audited read) by itself.
  it('reads the content of a hash only when asked', async () => {
    const [element, calls] = await mount({ approval: 'approved' });
    await expand(element);
    expect(calls.reads).to.deep.equal([]);
    expect(
      element.shadowRoot!.querySelector('extension-content-viewer')
    ).to.equal(null);
    const viewer = await read(element, 0);
    expect(calls.reads).to.deep.equal([H2]);
    expect(
      viewer.shadowRoot!.querySelector('.rendered')?.textContent
    ).to.contain('Never ask.');
    expect(element.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.contain(
      'Each read is recorded in the audit log'
    );
  });

  // protects=approving from the content view pins the hash being read;
  // fails_when=the pin names another hash or a name approval is written.
  it('pins the hash being read when the extension is already approved', async () => {
    const [element, calls] = await mount({ approval: 'approved' });
    await expand(element);
    const viewer = await read(element, 0);
    viewer.shadowRoot!.querySelector<HTMLElement>('.approve-content')!.click();
    await waitUntil(() => calls.pinned.length === 1);
    expect(calls.pinned).to.deep.equal([H2]);
    expect(calls.updates).to.deep.equal([]);
  });

  it('approves and pins in one request when there is no decision yet', async () => {
    const [element, calls] = await mount({ approval: 'unknown' });
    await expand(element);
    const viewer = await read(element, 0);
    viewer.shadowRoot!.querySelector<HTMLElement>('.approve-content')!.click();
    await waitUntil(() => calls.updates.length === 1);
    expect(calls.updates).to.deep.equal([['approved', H2]]);
    expect(calls.pinned).to.deep.equal([]);
  });

  it('diffs the pinned hash against the current one from the row', async () => {
    const [element, calls] = await mount({ approval: 'approved' });
    await expand(element);
    const viewer = await read(element, 0);
    viewer.shadowRoot!.querySelector<HTMLElement>('.compare')!.click();
    await waitUntil(() => viewer.shadowRoot!.querySelector('.diff'));
    expect(calls.reads).to.deep.equal([H2, H1]);
    const diff = viewer.shadowRoot!.querySelector('.diff')!;
    expect(diff.querySelector('.removed')?.textContent).to.contain(
      'Ask first.'
    );
    expect(diff.querySelector('.added')?.textContent).to.contain('Never ask.');
  });

  // protects=a device row reads the content of that device's hash and
  // approving there pins it; fails_when=the device view has no way to read.
  it('reads and approves the content of a device row', async () => {
    const calls: Calls = {
      reads: [],
      updates: [],
      pinned: [],
      acknowledged: 0,
    };
    const item: InstalledExtension = {
      agent_kind: 'claude_code',
      extension_kind: 'skill',
      extension_id: 'deploy',
      approval_state: 'approved',
      fingerprint: 'a'.repeat(64),
      profile_ref: 'current',
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
      effective_state: 'content_changed',
      content_hash: H2,
      content_hash_status: 'ok',
    };
    const element = await fixture<ExtensionInventory>(
      html`<extension-inventory
        .fingerprint=${'a'.repeat(64)}
        .loader=${async () => ({
          items: [item],
          next_cursor: null,
          completeness: 'complete',
        })}
        .accessLoader=${async () => ({ view: true, manage: true })}
        .updater=${async (_row: ExtensionKey, state: string, hash?: string) => {
          calls.updates.push([state, hash]);
        }}
        .pinLoader=${async () => [pin]}
        .pinAdder=${async (_row: ExtensionKey, hash: string) => {
          calls.pinned.push(hash);
          return pin;
        }}
        .contentLoader=${async (_kind: string, hash: string) => {
          calls.reads.push(hash);
          return stored(hash, '# Deploy\n\nNever ask.\n');
        }}
        .contentSettingsLoader=${async () => settings()}
        .contentAccessLoader=${async () => true}
      ></extension-inventory>`
    );
    await waitUntil(() => !!element.shadowRoot?.querySelector('.footer'));
    expect(calls.reads).to.deep.equal([]);
    // A single device's panel does not repeat the account-wide notice.
    expect(element.shadowRoot!.querySelector('.content-notice')).to.equal(null);
    element.shadowRoot!.querySelector<HTMLElement>('.toggle-details')!.click();
    await waitUntil(() =>
      element.shadowRoot!.querySelector('.extension-details')
    );
    expect(calls.reads).to.deep.equal([]);
    const viewer = await read(element, 0);
    expect(calls.reads).to.deep.equal([H2]);
    await waitUntil(() => viewer.shadowRoot!.querySelector('.compare'));
    viewer.shadowRoot!.querySelector<HTMLElement>('.approve-content')!.click();
    // The extension is already approved, so approving this content adds a pin.
    await waitUntil(() => calls.pinned.length === 1);
    expect(calls.pinned).to.deep.equal([H2]);
    expect(calls.updates).to.deep.equal([]);
  });

  it('offers no content read without the approval permission', async () => {
    const [element, calls] = await mount({
      approval: 'approved',
      manage: false,
    });
    await expand(element);
    expect(element.shadowRoot!.querySelector('.read-content')).to.equal(null);
    expect(calls.reads).to.deep.equal([]);
  });

  // protects=the one-time administrator banner for accounts that reported
  // under the old contract; fails_when=it is shown to non-admins, does not
  // name the setting, or cannot be dismissed.
  it('shows administrators a one-time notice that names the setting', async () => {
    const [element, calls] = await mount({ approval: 'unknown' });
    await waitUntil(() => element.shadowRoot!.querySelector('.content-notice'));
    const notice = element.shadowRoot!.querySelector('.content-notice')!;
    expect(notice.textContent).to.contain(
      'Collect extension content for review'
    );
    expect(notice.textContent).to.contain('reported before this');
    expect(notice.querySelector('a')?.getAttribute('href')).to.equal(
      '/console/settings/account#extension-content'
    );
    notice.querySelector<HTMLElement>('.dismiss-content-notice')!.click();
    await waitUntil(() => calls.acknowledged === 1);
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('.content-notice')).to.equal(null);
  });

  it('shows no notice once acknowledged, or to someone who cannot change the setting', async () => {
    const [done] = await mount({
      approval: 'unknown',
      settings: settings({ notice_pending: false }),
    });
    const [viewer] = await mount({ approval: 'unknown', admin: false });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(done.shadowRoot!.querySelector('.content-notice')).to.equal(null);
    expect(viewer.shadowRoot!.querySelector('.content-notice')).to.equal(null);
  });
});
