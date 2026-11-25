import {
  LitElement,
  css,
  html,
  nothing,
  type PropertyValues,
  type TemplateResult,
} from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { downloadText } from '../utils/records-format';
import {
  getEnforcementPolicy,
  saveEnforcementPolicy,
  renderEnforcementPolicy,
  getOverlayRegistrations,
  type EnforcementPolicy,
  type EnforcementOptions,
  type Marketplace,
  type OverlayPlatform,
  type RenderedOverlay,
  type OverlayRegistration,
} from '../extension-enforcement-api';

const options: Array<[keyof EnforcementOptions, string, string]> = [
  [
    'block_unapproved_plugins',
    'Block unapproved plugins',
    'Stops named marketplace plugins marked Unapproved; unknown plugins stay available.',
  ],
  [
    'restrict_marketplaces',
    'Restrict marketplaces',
    'Only allowed sources remain available, including official sources you list explicitly.',
  ],
  [
    'managed_hooks_only',
    'Managed hooks only',
    'Blocks user and project hooks. Deploy and register the Preloop managed hook bundle first.',
  ],
  [
    'block_user_project_skills',
    'Block user and project skills',
    'Keeps marketplace plugin skills; does not lock MCP servers or hooks.',
  ],
  [
    'disable_skill_shell',
    'Disable skill shell execution',
    'Stops inline shell commands in user, project and plugin skills.',
  ],
  [
    'disable_sideload_flags',
    'Disable sideload flags',
    'Rejects --plugin-dir, --plugin-url, --agents and --mcp-config at startup.',
  ],
];
const fields: Record<string, string[]> = {
  github: ['repo', 'ref', 'path'],
  git: ['url', 'ref', 'path'],
  url: ['url'],
  file: ['path'],
  directory: ['path'],
  hostPattern: ['hostPattern'],
  pathPattern: ['pathPattern'],
  'skills-dir': [],
};

/** Reviewable account restrictions; never installs or attests to device policy. */
@customElement('extension-enforcement')
export class ExtensionEnforcement extends LitElement {
  @property({ type: Boolean }) canManage = false;
  @property({ type: Number }) approvalRevision = 0;
  @property({ attribute: false }) policyLoader = getEnforcementPolicy;
  @property({ attribute: false }) registryLoader = getOverlayRegistrations;
  @property({ attribute: false }) saver = saveEnforcementPolicy;
  @property({ attribute: false }) renderer = renderEnforcementPolicy;
  @property({ attribute: false }) downloader = downloadText;
  @property({ attribute: false }) copier = (text: string): Promise<void> =>
    navigator.clipboard.writeText(text);
  @state() private policy: EnforcementPolicy | null = null;
  @state() private registrations: OverlayRegistration[] = [];
  @state() private overlay: RenderedOverlay | null = null;
  @state() private platform: OverlayPlatform = 'darwin';
  @state() private delivery = 'endpoint_file';
  @state() private busy = false;
  @state() private dirty = false;
  @state() private error = '';
  @state() private copied = false;
  static styles = css`
    :host {
      display: block;
      margin-top: 24px;
      font-size: var(--console-text-body);
    }
    summary {
      cursor: pointer;
      font-weight: 600;
      padding: 12px 0;
    }
    fieldset {
      border: 0;
      padding: 0;
      margin: 0;
      min-width: 0;
    }
    label {
      display: flex;
      gap: 8px;
      align-items: center;
      margin: 8px 0;
    }
    .consequence,
    .meta {
      color: var(--console-meta-color);
      font-size: var(--console-text-meta);
    }
    .consequence {
      margin: 0 0 12px 24px;
    }
    .marketplace {
      border-block: 1px solid var(--console-hairline);
      padding: 12px 0;
    }
    input,
    select,
    button {
      font: inherit;
      color: inherit;
      background: transparent;
      border: 1px solid var(--console-hairline);
      border-radius: 4px;
      padding: 6px 8px;
    }
    button {
      cursor: pointer;
    }
    button:disabled {
      cursor: default;
      opacity: 0.5;
    }
    input:not([type='checkbox']) {
      min-width: 0;
      flex: 1;
    }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 16px 0;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: var(--console-text-meta);
    }
    code {
      overflow-wrap: anywhere;
    }
    [role='alert'] {
      color: var(--sl-color-danger-700);
    }
  `;
  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('approvalRevision')) {
      this.overlay = null;
      this.copied = false;
    }
  }

  private async load(event: Event): Promise<void> {
    if (
      !(event.target as HTMLDetailsElement).open ||
      this.policy ||
      this.busy ||
      !this.canManage
    )
      return;
    await this.reload();
  }
  private async reload(): Promise<void> {
    if (this.busy || !this.canManage) return;
    this.busy = true;
    this.error = '';
    try {
      const [policy, registrations] = await Promise.all([
        this.policyLoader(),
        this.registryLoader(),
      ]);
      this.policy = policy;
      this.registrations = registrations;
      this.dirty = false;
      this.overlay = null;
      this.copied = false;
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Unable to load enforcement policy';
    } finally {
      this.busy = false;
    }
  }
  private changed(): void {
    this.dirty = true;
    this.overlay = null;
    this.copied = false;
    this.requestUpdate();
  }
  private editMarketplace(index: number, update: Partial<Marketplace>): void {
    if (!this.policy) return;
    this.policy = {
      ...this.policy,
      marketplaces: this.policy.marketplaces.map((m, i) =>
        i === index ? { ...m, ...update } : m
      ),
    };
    this.changed();
  }
  private async preview(): Promise<void> {
    if (!this.policy || this.busy || !this.canManage) return;
    const approvalRevision = this.approvalRevision;
    this.busy = true;
    this.error = '';
    this.overlay = null;
    this.copied = false;
    try {
      if (this.dirty) {
        this.policy = await this.saver(this.policy);
        this.dirty = false;
      }
      const [overlay, registrations] = await Promise.all([
        this.renderer(this.platform),
        this.registryLoader(),
      ]);
      if (approvalRevision === this.approvalRevision) this.overlay = overlay;
      else this.error = 'Approvals changed. Preview again before downloading.';
      this.registrations = registrations;
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Unable to preview policy';
    } finally {
      this.busy = false;
    }
  }
  private async copy(): Promise<void> {
    if (!this.overlay) return;
    try {
      await this.copier(this.overlay.settings_json);
      this.copied = true;
    } catch {
      this.error =
        'Unable to copy. Download the response and use settings_json.';
    }
  }
  private marketplace(m: Marketplace, index: number): TemplateResult {
    const registrable =
      !['hostPattern', 'pathPattern', 'skills-dir'].includes(m.source.source) &&
      m.state === 'allowed' &&
      !String(m.source.repo || '').endsWith('/*');
    return html`<div class="marketplace">
      <label
        >Name
        <input
          aria-label="Marketplace name"
          .value=${m.name}
          @input=${(e: Event) => this.editMarketplace(index, { name: (e.target as HTMLInputElement).value })}
      /></label>
      <label
        >Source type
        <select
          aria-label="Source type"
          .value=${m.source.source}
          @change=${(e: Event) => this.editMarketplace(index, { source: { source: (e.target as HTMLSelectElement).value }, register: false })}
        >
          ${Object.keys(fields).map((type) => html`<option value=${type}>${type}</option>`)}
        </select></label
      >
      ${(fields[m.source.source] || []).map(
        (field) =>
          html`<label
            >${field}${field === 'ref' || (field === 'path' && ['github', 'git'].includes(m.source.source)) ? ' (optional)' : ''}<input
              aria-label=${field}
              .value=${String(m.source[field] || '')}
              @input=${(e: Event) => {
                const source = { ...m.source };
                const value = (e.target as HTMLInputElement).value;
                if (value) source[field] = value;
                else delete source[field];
                this.editMarketplace(index, { source });
              }}
          /></label>`
      )}
      <label
        >State
        <select
          aria-label="Marketplace state"
          .value=${m.state}
          @change=${(e: Event) => this.editMarketplace(index, { state: (e.target as HTMLSelectElement).value as Marketplace['state'], register: false })}
        >
          <option value="allowed">Allowed</option>
          <option value="blocked">Blocked</option>
        </select></label
      >
      <label
        ><input
          type="checkbox"
          ?disabled=${!registrable}
          .checked=${m.register}
          @change=${(e: Event) => this.editMarketplace(index, { register: (e.target as HTMLInputElement).checked })}
        />
        Register on devices</label
      >
      <button
        type="button"
        @click=${() => {
          this.policy = {
            ...this.policy!,
            marketplaces: this.policy!.marketplaces.filter(
              (_, i) => i !== index
            ),
          };
          this.changed();
        }}
      >
        Remove marketplace
      </button>
    </div>`;
  }
  render(): TemplateResult | typeof nothing {
    if (!this.canManage) return nothing;
    const registered = this.registrations.find(
      (r) =>
        r.manifest.app_id === 'claude-code-extensions' &&
        r.manifest.platform === this.platform
    );
    return html`<details @toggle=${this.load}>
      <summary>Enforcement</summary>
      <p>
        Review account restrictions before deploying them. Name denials do not
        enforce content pins or version constraints. Export and registration do
        not prove installation.
      </p>
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      ${this.busy ? html`<p role="status">Loading…</p>` : nothing}
      ${
        this.policy
          ? html`<fieldset ?disabled=${this.busy}>
              <label
                >Platform
                <select
                  aria-label="Target platform"
                  .value=${this.platform}
                  @change=${(e: Event) => {
                    this.platform = (e.target as HTMLSelectElement)
                      .value as OverlayPlatform;
                    this.overlay = null;
                    this.copied = false;
                  }}
                >
                  <option value="darwin">macOS</option>
                  <option value="linux">Linux</option>
                  <option value="windows">Windows</option>
                </select></label
              >
              ${options.map(
                ([key, name, consequence]) =>
                  html`<label
                      ><input
                        type="checkbox"
                        data-option=${key}
                        .checked=${this.policy!.options[key]}
                        @change=${(e: Event) => {
                          this.policy = {
                            ...this.policy!,
                            options: {
                              ...this.policy!.options,
                              [key]: (e.target as HTMLInputElement).checked,
                            },
                          };
                          this.changed();
                        }}
                      />${name}</label
                    >
                    <p class="consequence">${consequence}</p>`
              )}
              <h3>Marketplaces</h3>
              <p class="meta">
                An empty allow-list would block every marketplace, including the
                official one; the preview refuses it. Pattern sources cannot
                register on devices.
              </p>
              ${this.policy.marketplaces.map((m, i) => this.marketplace(m, i))}
              <div class="actions">
                <button
                  type="button"
                  data-add
                  ?disabled=${this.policy.marketplaces.length >= 50}
                  @click=${() => {
                    this.policy = {
                      ...this.policy!,
                      marketplaces: [
                        ...this.policy!.marketplaces,
                        {
                          name: '',
                          state: 'allowed',
                          register: false,
                          source: { source: 'github' },
                        },
                      ],
                    };
                    this.changed();
                  }}
                >
                  Add marketplace
                </button>
                <button type="button" data-preview @click=${this.preview}>
                  ${this.dirty ? 'Save and preview' : 'Preview'}
                </button>
                <button type="button" data-reload @click=${this.reload}>
                  Reload saved policy (discard edits)
                </button>
              </div>
              <p class="meta">
                ${registered ? html`Last registered: <code>${registered.manifest.policy_revision_hash}</code> · ${registered.registered_at}` : 'No registered extension bundle for this platform.'}
                ${this.overlay && registered && this.overlay.policy_revision_hash !== registered.manifest.policy_revision_hash ? html`<strong>Out of date</strong>` : nothing}
              </p>
              ${
                this.overlay
                  ? html`<p>Target: <code>${this.overlay.target_path}</code></p>
                      <p class="meta">
                        Minimum supported Claude Code:
                        ${this.overlay.minimum_claude_code_version}. Review
                        existing managed sources before deployment.
                      </p>
                      <pre>${this.overlay.settings_json}</pre>
                      ${
                        this.overlay.omitted.length
                          ? html`<p>Omitted restrictions</p>
                              <ul>
                                ${this.overlay.omitted.map((o) => html`<li>${o.key} ${o.subject || ''}: ${o.reason}</li>`)}
                              </ul>`
                          : nothing
                      }
                      <button
                        type="button"
                        data-download
                        @click=${() => this.downloader('preloop-extension-overlay.json', `${JSON.stringify(this.overlay, null, 2)}\n`)}
                      >
                        Download renderer response
                      </button>
                      <label
                        >Delivery recorded in manifest
                        <select
                          aria-label="Delivery channel"
                          .value=${this.delivery}
                          @change=${(e: Event) => {
                            this.delivery = (
                              e.target as HTMLSelectElement
                            ).value;
                          }}
                        >
                          <option value="endpoint_file">Endpoint file</option>
                          <option value="vendor_console">
                            Vendor console (unverified)
                          </option>
                        </select></label
                      >
                      <p>
                        <code
                          >preloop agents managed-config claude-code-extensions
                          --from-file preloop-extension-overlay.json --output
                          ./extension-bundle --platform ${this.platform}
                          --delivery ${this.delivery} --dry-run</code
                        >
                      </p>
                      <p class="meta">
                        Remove --dry-run to export reviewed files. Registration
                        requires a separate online export with --register.
                      </p>
                      <details>
                        <summary>Vendor admin console (unverified)</summary>
                        <p>
                          Delivery is unverified for agents routed through
                          Preloop with a non-default ANTHROPIC_BASE_URL. Prefer
                          the endpoint file.
                        </p>
                        <p>
                          Organization settings → Claude Code → Managed
                          settings. This client-side control can be bypassed on
                          an unmanaged device, caches for up to 24 hours, and is
                          not delivered to cloud-provider sessions.
                        </p>
                        <button type="button" data-copy @click=${this.copy}>
                          Copy for Claude admin console</button
                        >${this.copied ? html`<span role="status"> Copied</span>` : nothing}
                      </details> `
                  : nothing
              }
            </fieldset>`
          : nothing
      }
    </details>`;
  }
}
