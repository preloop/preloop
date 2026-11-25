import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-enforcement';
import type { ExtensionEnforcement } from './extension-enforcement';
import type {
  EnforcementPolicy,
  RenderedOverlay,
} from '../extension-enforcement-api';

const policy = (): EnforcementPolicy => ({
  revision: 3,
  marketplaces: [],
  options: {
    block_unapproved_plugins: true,
    restrict_marketplaces: false,
    managed_hooks_only: false,
    block_user_project_skills: false,
    disable_skill_shell: false,
    disable_sideload_flags: false,
  },
});
const overlay: RenderedOverlay = {
  settings_json: '{"enabledPlugins":{"a@m":false}}\n',
  policy_revision_hash: 'a'.repeat(64),
  keys: ['enabledPlugins'],
  omitted: [
    {
      key: 'enabledPlugins',
      subject: 'hash:1234',
      reason: 'not a marketplace plugin id',
    },
  ],
  platform: 'darwin',
  target_path:
    '/Library/Application Support/ClaudeCode/managed-settings.d/50-preloop-extensions.json',
  minimum_claude_code_version: '2.1.332',
  vendor_console_status: 'unverified_for_preloop_routed_agents',
  inputs: { policy_revision: 3, approval_count: 1, approvals_updated_at: null },
};
async function mount(
  extra: Partial<ExtensionEnforcement> = {}
): Promise<ExtensionEnforcement> {
  const el = await fixture<ExtensionEnforcement>(
    html`<extension-enforcement></extension-enforcement>`
  );
  Object.assign(
    el,
    {
      canManage: true,
      policyLoader: async () => policy(),
      registryLoader: async () => [
        {
          manifest: {
            app_id: 'claude-code-extensions',
            platform: 'darwin',
            policy_revision_hash: 'b'.repeat(64),
          },
          registered_at: '2026-11-25T12:00:00Z',
        },
      ],
      renderer: async () => overlay,
      saver: async (p: EnforcementPolicy) => ({
        ...p,
        revision: p.revision + 1,
      }),
    },
    extra
  );
  await el.updateComplete;
  (el.shadowRoot!.querySelector('summary') as HTMLElement).click();
  await waitUntil(() => !!el.shadowRoot!.querySelector('[data-preview]'));
  return el;
}

describe('extension enforcement', () => {
  // protects=AC10 permission gating; fails_when=non-managers see controls;
  // no_claim=server permissions tested separately.
  it('has no controls without manage_agents', async () => {
    const el = await fixture<ExtensionEnforcement>(
      html`<extension-enforcement></extension-enforcement>`
    );
    expect(el.shadowRoot!.querySelector('button')).to.equal(null);
    expect(el.shadowRoot!.textContent?.trim()).to.equal('');
  });
  // protects=AC10 options/editor, preview/omissions and stale registration;
  // fails_when=controls or out-of-date state disappear; no_claim=fixture APIs.
  it('renders six options, marketplace editor, preview and registration status', async () => {
    const el = await mount();
    expect(el.shadowRoot!.querySelectorAll('[data-option]').length).to.equal(6);
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('pre'));
    expect(el.shadowRoot!.textContent).to.include('Out of date');
    expect(el.shadowRoot!.textContent).to.include('hash:1234');
    (el.shadowRoot!.querySelector('[data-add]') as HTMLButtonElement).click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[aria-label="Marketplace name"]')
    ).not.to.equal(null);
    expect(el.shadowRoot!.querySelector('[data-download]')).to.equal(null);
  });
  // protects=AC10 saved response download and AC11a honest console label;
  // fails_when=only settings download or console verification claimed; no_claim=vendor delivery.
  it('downloads the complete response and warns before vendor-console copy', async () => {
    const saved: string[] = [];
    const copied: string[] = [];
    const el = await mount({
      downloader: (_name: string, data: string) => saved.push(data),
      copier: async (text: string) => {
        copied.push(text);
      },
    });
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('[data-download]'));
    (
      el.shadowRoot!.querySelector('[data-download]') as HTMLButtonElement
    ).click();
    expect(JSON.parse(saved[0])).to.deep.equal(overlay);
    expect(el.shadowRoot!.textContent?.replace(/\s+/g, ' ')).to.include(
      'unverified for agents routed through Preloop'
    );
    (el.shadowRoot!.querySelector('[data-copy]') as HTMLButtonElement).click();
    await waitUntil(() => copied.length === 1);
    expect(copied[0]).to.equal(overlay.settings_json);
  });
  // protects=AC10 actionable refusals; fails_when=server explanation replaced;
  // no_claim=fixture rejects as server 409/422.
  it('shows the server refusal and clears an old preview', async () => {
    const el = await mount({
      renderer: async () => {
        throw new Error(
          'An empty marketplace allow-list locks out the official marketplace'
        );
      },
    });
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(() => el.shadowRoot!.textContent!.includes('locks out'));
    expect(el.shadowRoot!.querySelector('[data-download]')).to.equal(null);
  });
  // protects=AC10 platform-specific downloads/commands; fails_when=old platform
  // artifact remains downloadable; no_claim=fixture renderer, no CLI invocation.
  it('invalidates preview when the platform changes', async () => {
    const platforms: string[] = [];
    const el = await mount({
      renderer: async (platform) => {
        platforms.push(platform);
        return { ...overlay, platform };
      },
    });
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('[data-download]'));
    const select = el.shadowRoot!.querySelector(
      '[aria-label="Target platform"]'
    ) as HTMLSelectElement;
    select.value = 'linux';
    select.dispatchEvent(new Event('change'));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-download]')).to.equal(null);
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(
      () =>
        platforms.length === 2 &&
        !!el.shadowRoot!.querySelector('[data-download]')
    );
    expect(platforms).to.deep.equal(['darwin', 'linux']);
    expect(el.shadowRoot!.textContent).to.include('--platform linux');
  });
  // protects=approval edits invalidate the downloadable snapshot; fails_when=
  // an old overlay remains available after a decision; no_claim=local view edits.
  it('invalidates preview after an account approval changes', async () => {
    const el = await mount();
    (
      el.shadowRoot!.querySelector('[data-preview]') as HTMLButtonElement
    ).click();
    await waitUntil(() => !!el.shadowRoot!.querySelector('[data-download]'));
    Object.assign(el, { approvalRevision: 1 });
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('[data-download]')).to.equal(null);
  });
});
