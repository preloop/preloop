import { LitElement, css, html, nothing } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import { confirmDialog } from './confirm-dialog';
import {
  canManageExtensionContent,
  getExtensionContentSettings,
  updateExtensionContentSettings,
  type ExtensionContentSettings,
} from '../extension-content-api';

/**
 * The account switch "Collect extension content for review".
 *
 * On by default. Turning it off deletes what is stored, so it asks first
 * and says how many contents go.
 */
@customElement('extension-content-setting')
export class ExtensionContentSetting extends LitElement {
  @property({ attribute: false }) loader = getExtensionContentSettings;
  @property({ attribute: false }) saver = updateExtensionContentSettings;
  @property({ attribute: false }) accessLoader = canManageExtensionContent;
  @property({ attribute: false }) confirm = confirmDialog;
  @state() private settings: ExtensionContentSettings | null = null;
  @state() private canManage = false;
  @state() private saving = false;
  @state() private error = '';

  static styles = css`
    :host {
      display: block;
      font-size: var(--console-text-body);
    }
    .meta {
      color: var(--console-meta-color);
      font-size: var(--console-text-meta);
      margin: 4px 0 0;
      max-width: 720px;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const [settings, canManage] = await Promise.all([
        this.loader(),
        this.accessLoader().catch(() => false),
      ]);
      this.settings = settings;
      this.canManage = canManage;
    } catch {
      this.settings = null;
    }
  }

  private async toggle(event: Event): Promise<void> {
    const control = event.target as HTMLInputElement;
    const settings = this.settings;
    if (!settings) return;
    const enable = control.checked;
    if (!enable) {
      const confirmed = await this.confirm({
        title: 'Stop collecting extension content?',
        message: `Devices will stop uploading extension content, and the ${settings.stored_contents} stored content(s) of this account are deleted now. Approvals, pins and the inventory are kept.`,
        confirmLabel: 'Turn off and delete',
        variant: 'danger',
      });
      if (!confirmed) {
        control.checked = true;
        return;
      }
    }
    this.saving = true;
    this.error = '';
    try {
      this.settings = await this.saver({ collect_enabled: enable });
      this.dispatchEvent(
        new CustomEvent('extension-content-setting-changed', {
          detail: this.settings,
          bubbles: true,
          composed: true,
        })
      );
    } catch {
      this.error = 'Could not save the setting.';
      control.checked = settings.collect_enabled;
    } finally {
      this.saving = false;
    }
  }

  render(): TemplateResult | typeof nothing {
    const settings = this.settings;
    if (!settings) return nothing;
    return html`
      <sl-switch
        class="collect"
        ?checked=${settings.collect_enabled}
        ?disabled=${this.saving || !this.canManage}
        @sl-change=${this.toggle}
        >Collect extension content for review</sl-switch
      >
      <p class="meta">
        When on, devices that run discovery reporting upload the text of skills,
        commands, hook commands and plugin files so an approver can read what
        they approve. Text is redacted on the device first, stored encrypted,
        readable by people who can approve extensions, and deleted
        ${settings.retention_days} days after the last report. Redaction reduces
        the chance that a secret is stored; it is not a guarantee.
        ${settings.collect_enabled ? `${settings.stored_contents} content(s) stored.` : 'Off: devices send no content and nothing is stored.'}
        ${this.canManage ? '' : 'Only an account admin can change this.'}
      </p>
      ${this.error ? html`<p class="meta" role="alert">${this.error}</p>` : nothing}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'extension-content-setting': ExtensionContentSetting;
  }
}
