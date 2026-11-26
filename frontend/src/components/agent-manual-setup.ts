import { LitElement, css, html, nothing } from 'lit';
import type { PropertyValues, TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import {
  ApiError,
  cancelAgentPendingEnrollment,
  createManagedAgentCredential,
  getAgentManualSetup,
} from '../api';
import type {
  AgentManualSetupFile,
  AgentManualSetupInstructions,
  AgentManualSetupResponse,
  AgentOnboardingCleanupRemoved,
} from '../api';
import { formatAbsoluteTime } from '../utils/date';

export const MANUAL_SETUP_DOCS_URL =
  'https://docs.preloop.ai/guide/clients/sandboxed-agents/';

const KEY_PLACEHOLDER = '<AGENT_KEY>';
const KEY_TAIL_PLACEHOLDER = '<AGENT_KEY_LAST_20>';
const KEY_TAIL_LENGTH = 20;

/** Detail of `manual-setup-cancelled`: what the cancel removed. */
export interface ManualSetupCancelledDetail {
  agentId: string;
  enrollmentId: string;
  removed: AgentOnboardingCleanupRemoved;
}

/** Detail of `manual-setup-changed`: the pending setup ended elsewhere. */
export interface ManualSetupChangedDetail {
  agentId: string;
}

/** Detail of `manual-setup-key-issued`. Never carries the key itself. */
export interface ManualSetupKeyIssuedDetail {
  agentId: string;
  credentialId: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function fillPlaceholders(text: string, key: string): string {
  return text
    .split(KEY_TAIL_PLACEHOLDER)
    .join(key.slice(-KEY_TAIL_LENGTH))
    .split(KEY_PLACEHOLDER)
    .join(key);
}

/**
 * Replace the `<AGENT_KEY>` and `<AGENT_KEY_LAST_20>` placeholders with a
 * real key, in every string and object key of a JSON-like value.
 *
 * @param value - Any JSON-like value from the manual setup response.
 * @param key - The issued key, or null to leave the placeholders in place.
 * @returns A new value with the key filled in, or `value` itself without one.
 */
export function substituteAgentKey<T>(value: T, key: string | null): T {
  if (!key) return value;
  const walk = (node: unknown): unknown => {
    if (typeof node === 'string') return fillPlaceholders(node, key);
    if (Array.isArray(node)) return node.map(walk);
    if (isPlainObject(node)) {
      return Object.fromEntries(
        Object.entries(node).map(([name, child]) => [
          fillPlaceholders(name, key),
          walk(child),
        ])
      );
    }
    return node;
  };
  return walk(value) as T;
}

function tomlString(value: string): string {
  const escaped = value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')
    .replace(
      // Remaining control characters have no short escape in TOML.
      /[\u0000-\u001f\u007f]/g,
      (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`
    );
  return `"${escaped}"`;
}

function tomlKey(key: string): string {
  return /^[A-Za-z0-9_-]+$/.test(key) ? key : tomlString(key);
}

function isTableArray(value: unknown): value is Record<string, unknown>[] {
  return Array.isArray(value) && value.length > 0 && value.every(isPlainObject);
}

function tomlInline(value: unknown): string {
  if (typeof value === 'string') return tomlString(value);
  if (typeof value === 'number' || typeof value === 'bigint') {
    return Number.isFinite(Number(value)) ? String(value) : tomlString('');
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (Array.isArray(value)) {
    return `[${value
      .filter((item) => item !== null && item !== undefined)
      .map(tomlInline)
      .join(', ')}]`;
  }
  if (isPlainObject(value)) {
    const pairs = Object.entries(value)
      .filter(([, child]) => child !== null && child !== undefined)
      .map(([name, child]) => `${tomlKey(name)} = ${tomlInline(child)}`);
    return pairs.length ? `{ ${pairs.join(', ')} }` : '{}';
  }
  return tomlString(String(value));
}

/**
 * Serialize a JSON-like object as TOML.
 *
 * Covers what agent config files use: strings, numbers, booleans, arrays,
 * nested tables (`[mcp_servers.name.http_headers]`) and arrays of tables.
 * Null values are skipped, since TOML has no null. A table that holds only
 * other tables gets no header of its own.
 *
 * @param value - The object to write.
 * @returns TOML text ending in a newline, or '' for an empty object.
 */
export function toToml(value: Record<string, unknown>): string {
  const blocks: string[] = [];

  const writeTable = (
    table: Record<string, unknown>,
    path: string[],
    header: string | null,
    keepHeader = false
  ): void => {
    const entries = Object.entries(table).filter(
      ([, child]) => child !== null && child !== undefined
    );
    const lines = entries
      .filter(([, child]) => !isPlainObject(child) && !isTableArray(child))
      .map(([name, child]) => `${tomlKey(name)} = ${tomlInline(child)}`);
    // A table that only holds other tables needs no header of its own.
    const onlySubtables = lines.length === 0 && entries.length > 0;
    if (header !== null && (keepHeader || !onlySubtables)) {
      lines.unshift(header);
    }
    if (lines.length) blocks.push(lines.join('\n'));

    for (const [name, child] of entries) {
      const childPath = [...path, tomlKey(name)];
      const dotted = childPath.join('.');
      if (isPlainObject(child)) {
        writeTable(child, childPath, `[${dotted}]`);
      } else if (isTableArray(child)) {
        // Each array item starts at its own header, even an empty one.
        for (const item of child) {
          writeTable(item, childPath, `[[${dotted}]]`, true);
        }
      }
    }
  };

  writeTable(value, [], null);
  return blocks.length ? `${blocks.join('\n\n')}\n` : '';
}

/** `export KEY='value'` lines, safe to paste into a POSIX shell. */
function envExports(env: Record<string, string>): string {
  return Object.entries(env)
    .map(
      ([name, value]) =>
        `export ${name}='${String(value).replace(/'/g, `'\\''`)}'`
    )
    .join('\n');
}

function fileBlock(file: AgentManualSetupFile): string {
  const merge = isPlainObject(file.merge) ? file.merge : {};
  if (file.format === 'toml') return toToml(merge);
  return JSON.stringify(merge, null, 2);
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** yyyymmddhhmmss in UTC, for a unique credential name. */
function timestampSuffix(now = new Date()): string {
  return now.toISOString().replace(/\D/g, '').slice(0, 14);
}

/**
 * The config change a user applies by hand to route one agent through
 * Preloop, for agents whose config file the CLI cannot write (containers,
 * read-only mounts).
 *
 * Shows a setup that is waiting for local configuration, with the command
 * that resumes it and a way to cancel it. A key issued here is filled into
 * the snippets and lives only in this element's state: it is not persisted,
 * and it is dropped when the element leaves the page.
 *
 * @fires manual-setup-cancelled - A pending setup was cancelled. Detail:
 *   {@link ManualSetupCancelledDetail}.
 * @fires manual-setup-changed - The pending setup had already ended. Detail:
 *   {@link ManualSetupChangedDetail}.
 * @fires manual-setup-key-issued - A key was issued. Detail:
 *   {@link ManualSetupKeyIssuedDetail}, without the key.
 */
@customElement('agent-manual-setup')
export class AgentManualSetup extends LitElement {
  @property({ attribute: 'agent-id' }) agentId = '';
  @property({ attribute: 'display-name' }) displayName = '';
  /** Hide the actions that change something (issue a key, cancel). */
  @property({ type: Boolean, attribute: 'read-only' }) readOnly = false;

  @state() private setup: AgentManualSetupResponse | null = null;
  @state() private loading = true;
  @state() private loadError: string | null = null;
  @state() private actionError: string | null = null;
  /** The issued key. Component state only; see disconnectedCallback. */
  @state() private issuedKey: string | null = null;
  @state() private issuing = false;
  @state() private confirmingCancel = false;
  @state() private cancelling = false;
  @state() private cancelled: AgentOnboardingCleanupRemoved | null = null;

  private loadGeneration = 0;

  static styles = css`
    :host {
      display: block;
      width: 100%;
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-small);
    }

    .panel {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }

    .loading {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-x-small);
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }

    h3,
    h4 {
      margin: 0;
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-semibold);
    }

    p {
      margin: 0;
    }

    .stack {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
    }

    .meta {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sl-spacing-x-small);
    }

    .file {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-small);
    }

    .file-path {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-weight: var(--sl-font-weight-semibold);
    }

    code,
    pre {
      font-family: var(--console-font-mono, var(--sl-font-mono));
      font-size: 0.8125rem;
    }

    code {
      overflow-wrap: anywhere;
    }

    .code-block {
      position: relative;
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
    }

    .code-block pre {
      margin: 0;
      padding: var(--sl-spacing-small);
      padding-right: var(--sl-spacing-2x-large);
      overflow-x: auto;
      white-space: pre;
    }

    .code-block sl-copy-button {
      position: absolute;
      top: 4px;
      right: 4px;
    }

    ul {
      margin: 0;
      padding-left: var(--sl-spacing-large);
    }

    a {
      color: var(--sl-color-primary-700);
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    // The first load follows the agent id in willUpdate; this is a re-attach.
    if (this.hasUpdated && this.agentId) void this.load();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    // The key is shown once: it does not outlive the element on the page.
    this.forgetKey();
    this.loadGeneration += 1;
  }

  protected willUpdate(changed: PropertyValues<this>): void {
    if (changed.has('agentId')) {
      // A key belongs to one agent; never carry it to another.
      this.forgetKey();
      this.setup = null;
      this.cancelled = null;
      this.actionError = null;
      void this.load();
    }
  }

  private forgetKey(): void {
    this.issuedKey = null;
    this.confirmingCancel = false;
  }

  /** Error text for display, with the issued key removed if it appears. */
  private describeError(error: unknown, fallback: string): string {
    const message =
      error instanceof Error && error.message ? error.message : fallback;
    return this.issuedKey
      ? message.split(this.issuedKey).join(KEY_PLACEHOLDER)
      : message;
  }

  private async load(): Promise<void> {
    const generation = ++this.loadGeneration;
    const agentId = this.agentId;
    if (!agentId) {
      this.loading = false;
      return;
    }
    this.loading = true;
    this.loadError = null;
    try {
      const setup = await getAgentManualSetup(agentId);
      if (generation !== this.loadGeneration) return;
      this.setup = setup;
    } catch (error) {
      if (generation !== this.loadGeneration) return;
      this.setup = null;
      this.loadError = this.describeError(
        error,
        'Could not load manual setup.'
      );
    } finally {
      if (generation === this.loadGeneration) this.loading = false;
    }
  }

  private emit<T>(name: string, detail: T): void {
    this.dispatchEvent(
      new CustomEvent<T>(name, { detail, bubbles: true, composed: true })
    );
  }

  private async issueKey(): Promise<void> {
    if (this.issuing || this.issuedKey || !this.agentId) return;
    const agentId = this.agentId;
    this.issuing = true;
    this.actionError = null;
    try {
      const result = await createManagedAgentCredential(agentId, {
        name: `manual-setup-${timestampSuffix()}`,
        description: 'Issued from the console for manual setup',
      });
      if (agentId !== this.agentId || !this.isConnected) return;
      this.issuedKey = result.token;
      this.emit<ManualSetupKeyIssuedDetail>('manual-setup-key-issued', {
        agentId,
        credentialId: result.credential.id,
      });
    } catch (error) {
      this.actionError = this.describeError(error, 'Could not issue a key.');
    } finally {
      this.issuing = false;
    }
  }

  private async cancelSetup(): Promise<void> {
    const pending = this.setup?.pending;
    if (this.cancelling || !pending || !this.agentId) return;
    const agentId = this.agentId;
    this.cancelling = true;
    this.actionError = null;
    try {
      const result = await cancelAgentPendingEnrollment(
        agentId,
        pending.enrollment_id
      );
      this.cancelled = result.removed;
      this.confirmingCancel = false;
      if (this.setup) this.setup = { ...this.setup, pending: null };
      this.emit<ManualSetupCancelledDetail>('manual-setup-cancelled', {
        agentId,
        enrollmentId: pending.enrollment_id,
        removed: result.removed,
      });
    } catch (error) {
      this.confirmingCancel = false;
      if (error instanceof ApiError && error.status === 409) {
        this.actionError =
          'This setup is no longer pending, so there is nothing to cancel. It was completed, cancelled or expired in the meantime.';
        this.emit<ManualSetupChangedDetail>('manual-setup-changed', {
          agentId,
        });
        await this.load();
      } else {
        this.actionError = this.describeError(
          error,
          'Could not cancel the setup.'
        );
      }
    } finally {
      this.cancelling = false;
    }
  }

  private get agentName(): string {
    return this.displayName || this.setup?.display_name || 'this agent';
  }

  private renderCodeBlock(text: string, copyLabel: string): TemplateResult {
    return html`<div class="code-block">
      <pre>${text}</pre>
      <sl-copy-button value=${text} copy-label=${copyLabel}></sl-copy-button>
    </div>`;
  }

  private renderDocsLink(): TemplateResult {
    return html`<p class="meta">
      More on agents that cannot have their config written for them:
      <a
        data-manual-setup-docs
        href=${MANUAL_SETUP_DOCS_URL}
        target="_blank"
        rel="noopener noreferrer"
        >Sandboxed and containerised agents guide</a
      >
    </p>`;
  }

  private renderPending(): TemplateResult | typeof nothing {
    const pending = this.setup?.pending;
    if (!pending || !this.setup) return nothing;
    const expires = formatAbsoluteTime(pending.expires_at);
    return html`<sl-alert open variant="warning" data-manual-setup-pending>
      <sl-icon slot="icon" name="hourglass-split"></sl-icon>
      <div class="stack">
        <strong data-manual-setup-pending-title
          >Waiting for local configuration</strong
        >
        <p>
          ${this.agentName} is not onboarded until the change below is applied
          where the agent runs. Preloop could not write the agent's config file,
          so nothing is routed through Preloop yet.
        </p>
        <p data-manual-setup-expiry>
          ${
            expires
              ? html`This setup expires on ${expires}. After that, Preloop
                removes the key and what onboarding created.`
              : html`When this setup expires, Preloop removes the key and what
                onboarding created.`
          }
        </p>
        <p>After applying the change, run this where the agent runs:</p>
        ${this.renderCodeBlock(
          this.setup.resume_command,
          'Copy resume command'
        )}
        ${this.renderCancelControls()}
      </div>
    </sl-alert>`;
  }

  private renderCancelControls(): TemplateResult | typeof nothing {
    if (this.readOnly) return nothing;
    if (!this.confirmingCancel) {
      return html`<div class="actions">
        <sl-button
          size="small"
          data-manual-setup-cancel
          @click=${() => {
            this.confirmingCancel = true;
          }}
          >Cancel setup</sl-button
        >
      </div>`;
    }
    return html`<div class="stack" data-manual-setup-cancel-confirmation>
      <p>
        <strong>Cancel this setup?</strong> Preloop removes the key and the
        models that onboarding created. An agent that onboarding created and
        that was never used is deleted.
      </p>
      <div class="actions">
        <sl-button
          size="small"
          variant="danger"
          data-manual-setup-cancel-confirm
          ?loading=${this.cancelling}
          ?disabled=${this.cancelling}
          @click=${() => void this.cancelSetup()}
          >Cancel setup</sl-button
        >
        <sl-button
          size="small"
          data-manual-setup-cancel-keep
          ?disabled=${this.cancelling}
          @click=${() => {
            this.confirmingCancel = false;
          }}
          >Keep waiting</sl-button
        >
      </div>
    </div>`;
  }

  private renderCancelled(): TemplateResult | typeof nothing {
    const removed = this.cancelled;
    if (!removed) return nothing;
    const agentOutcome =
      removed.agent === 'deleted'
        ? 'The agent was deleted, because this setup created it and it was never used.'
        : removed.agent === 'decommissioned'
          ? 'The agent was decommissioned, because it is left without a usable key.'
          : 'The agent was kept.';
    return html`<sl-alert open variant="success" data-manual-setup-cancelled>
      <sl-icon slot="icon" name="check2-circle"></sl-icon>
      <strong>Setup cancelled</strong><br />
      Removed ${plural(removed.credentials, 'key', 'keys')} and
      ${plural(removed.ai_models, 'model', 'models')}. ${agentOutcome}
    </sl-alert>`;
  }

  private renderKeySection(
    instructions: AgentManualSetupInstructions
  ): TemplateResult {
    const usesTail =
      JSON.stringify(instructions).includes(KEY_TAIL_PLACEHOLDER);
    if (this.issuedKey) {
      return html`<sl-alert open variant="warning" data-manual-setup-key-notice>
        <sl-icon slot="icon" name="key"></sl-icon>
        <div class="stack">
          <strong>This key is shown once</strong>
          <p>
            It is filled in below. Copy what you need now: the key cannot be
            shown again, only replaced by issuing a new one. It disappears when
            you leave this page.
          </p>
          <div class="file-path">
            <code data-manual-setup-key>${this.issuedKey}</code>
            <sl-copy-button
              value=${this.issuedKey}
              copy-label="Copy key"
            ></sl-copy-button>
          </div>
          <div class="actions">
            <sl-button
              size="small"
              data-manual-setup-hide-key
              @click=${() => this.forgetKey()}
              >Hide key</sl-button
            >
          </div>
        </div>
      </sl-alert>`;
    }
    return html`<div class="stack" data-manual-setup-key-help>
      <h4>Agent key</h4>
      ${
        this.setup?.pending
          ? html`<p>
              The key the CLI printed is the one this pending setup expects: put
              it where the change below says <code>${KEY_PLACEHOLDER}</code>. A
              key issued here also works, because the resume command checks the
              configuration with a real request, so issuing one is optional.
            </p>`
          : html`<p>
              The change below shows <code>${KEY_PLACEHOLDER}</code> where the
              agent's key goes. Issue a key here to have it filled in, or put in
              a key you already hold for this agent.
            </p>`
      }
      ${
        usesTail
          ? html`<p class="meta">
              <code>${KEY_TAIL_PLACEHOLDER}</code> stands for the last
              ${KEY_TAIL_LENGTH} characters of the key.
            </p>`
          : nothing
      }
      ${
        this.readOnly
          ? nothing
          : html`<div class="actions">
              <sl-button
                size="small"
                variant="primary"
                data-manual-setup-issue
                ?loading=${this.issuing}
                ?disabled=${this.issuing}
                @click=${() => void this.issueKey()}
                >Issue a key and fill it in</sl-button
              >
              <span class="meta"
                >This adds a new key to the agent. Existing keys keep
                working.</span
              >
            </div>`
      }
    </div>`;
  }

  private renderFile(file: AgentManualSetupFile): TemplateResult {
    const remove = (file.remove ?? []).filter(Boolean);
    const formatName = file.format === 'toml' ? 'TOML' : 'JSON';
    return html`<div class="file stack" data-manual-setup-file>
      <div class="file-path">
        <code>${file.path}</code>
        <sl-copy-button
          value=${file.path}
          copy-label="Copy file path"
        ></sl-copy-button>
      </div>
      ${file.label ? html`<p>${file.label}</p>` : nothing}
      ${file.reason ? html`<p class="meta">${file.reason}</p>` : nothing}
      ${
        file.host_source
          ? html`<p class="meta" data-manual-setup-host-source>
              On the host this is probably <code>${file.host_source}</code>
            </p>`
          : nothing
      }
      <p>
        ${
          file.exists
            ? 'Merge these keys and keep everything else'
            : 'Create the file with exactly this content'
        }
        (${formatName}):
      </p>
      ${this.renderCodeBlock(fileBlock(file), `Copy ${formatName}`)}
      ${
        remove.length
          ? html`<div class="stack" data-manual-setup-remove>
              <p>Remove these keys from the file if they are present:</p>
              <ul>
                ${remove.map((key) => html`<li><code>${key}</code></li>`)}
              </ul>
            </div>`
          : nothing
      }
    </div>`;
  }

  private renderInstructions(): TemplateResult | typeof nothing {
    if (!this.setup) return nothing;
    if (this.cancelled?.agent === 'deleted') return nothing;
    const raw = this.setup.instructions;
    if (!raw) {
      return html`<p data-manual-setup-unavailable>
        Manual setup is not available for this agent type yet. The
        <a
          href=${MANUAL_SETUP_DOCS_URL}
          target="_blank"
          rel="noopener noreferrer"
          >guide for sandboxed agents</a
        >
        lists what is supported.
      </p>`;
    }
    const instructions = substituteAgentKey(raw, this.issuedKey);
    const files = instructions.files ?? [];
    const env = instructions.env ?? {};
    const hasEnv = Object.keys(env).length > 0;
    const limitations = (instructions.limitations ?? []).filter(Boolean);
    return html`
      ${
        this.setup.pending
          ? nothing
          : html`<p>
              Use this when ${this.agentName} runs where its config file is
              read-only or managed for you, for example in a container. Apply
              the change where the agent runs.
            </p>`
      }
      ${this.renderKeySection(raw)}
      ${
        files.length
          ? html`<div class="stack">
              <h4>${files.length === 1 ? 'Config file' : 'Config files'}</h4>
              ${files.map((file) => this.renderFile(file))}
            </div>`
          : nothing
      }
      ${
        hasEnv
          ? html`<div class="stack" data-manual-setup-env>
              <h4>Environment variables</h4>
              <p>
                ${
                  files.length
                    ? 'If you cannot change the file, set these in the environment the agent starts in:'
                    : 'Set these in the environment the agent starts in:'
                }
              </p>
              ${this.renderCodeBlock(envExports(env), 'Copy exports')}
            </div>`
          : nothing
      }
      ${
        instructions.settings_flag
          ? html`<div class="stack" data-manual-setup-settings-flag>
              <h4>Settings flag</h4>
              <p>
                Or keep the change in a separate file and start the agent with
                <code>${instructions.settings_flag}</code>
              </p>
            </div>`
          : nothing
      }
      ${
        limitations.length
          ? html`<div class="stack" data-manual-setup-limitations>
              <h4>What each form does not enable</h4>
              <ul>
                ${limitations.map((item) => html`<li>${item}</li>`)}
              </ul>
            </div>`
          : nothing
      }
    `;
  }

  render() {
    if (this.loading && !this.setup) {
      return html`<div class="panel">
        <div class="loading" data-manual-setup-loading role="status">
          <sl-spinner></sl-spinner> Loading manual setup…
        </div>
      </div>`;
    }
    if (this.loadError && !this.cancelled) {
      return html`<div class="panel">
        <sl-alert open variant="danger" role="alert" data-manual-setup-error>
          <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
          <strong>Could not load manual setup</strong><br />
          ${this.loadError}
          <div style="margin-top: var(--sl-spacing-small);">
            <sl-button
              size="small"
              data-manual-setup-retry
              @click=${() => void this.load()}
            >
              <sl-icon slot="prefix" name="arrow-clockwise"></sl-icon>
              Try again
            </sl-button>
          </div>
        </sl-alert>
        ${this.renderDocsLink()}
      </div>`;
    }
    return html`<div class="panel">
      ${this.renderPending()} ${this.renderCancelled()}
      ${
        this.actionError
          ? html`<sl-alert
              open
              variant="danger"
              role="alert"
              data-manual-setup-action-error
            >
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              ${this.actionError}
            </sl-alert>`
          : nothing
      }
      ${this.renderInstructions()} ${this.renderDocsLink()}
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'agent-manual-setup': AgentManualSetup;
  }
}
