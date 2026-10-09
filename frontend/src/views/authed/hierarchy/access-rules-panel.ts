import { tableScrollStyles } from '../../../styles/table-scroll';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import { isCapabilityOff } from '../../../capabilities';
import {
  applyAccessRulesYaml,
  explainAccess,
  exportAccessRulesYaml,
  getAccessRules,
  isConflict,
  previewMode,
  saveAccessRules,
  setMode,
  type AccessRule,
  type AccessRuleSet,
  type ExplainResult,
  type ModePreview,
  type RuleMode,
} from '../../../hierarchy-api';
import { parseTags } from './tags';

/** Actions whose mode an account can change. `resource:share` has none. */
export const MODE_ACTIONS = [
  'model:invoke',
  'tool:call',
  'flow:run',
  'runner:accept',
  'resource:view',
] as const;

const ALL_ACTIONS = [...MODE_ACTIONS, 'resource:share'];

/**
 * Policies > Access rules (capability `abac_rules`): this account's rules as
 * a form and as YAML, the parent's rules read-only, an Explain box, and the
 * per-action mode. Moving an action to `require_permit` can remove access,
 * so the control only saves after the preview of who would lose access has
 * been shown.
 */
@customElement('access-rules-panel')
export class AccessRulesPanel extends LitElement {
  static styles = [
    tableScrollStyles,
    css`
      :host {
        display: block;
      }
      section {
        margin-bottom: var(--sl-spacing-x-large);
      }
      h3 {
        font-size: var(--sl-font-size-medium);
        margin: 0 0 var(--sl-spacing-small);
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-small);
        border-bottom: 1px solid var(--sl-color-neutral-200);
        vertical-align: top;
      }
      .form {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
        gap: var(--sl-spacing-small);
        align-items: end;
      }
      .row {
        display: flex;
        gap: var(--sl-spacing-x-small);
        align-items: flex-end;
        flex-wrap: wrap;
      }
      .error {
        color: var(--sl-color-danger-700);
      }
      .preview {
        background: var(--sl-color-warning-50);
        padding: var(--sl-spacing-small);
        margin-top: var(--sl-spacing-x-small);
      }
    `,
  ];

  @property({ attribute: false }) context: Record<string, unknown> = {};

  @state() private data: AccessRuleSet | null = null;
  @state() private yaml = '';
  @state() private error = '';
  @state() private explain: ExplainResult | null = null;
  /** Mode chosen in the control, per action, before it is saved. */
  @state() private draftModes: Record<string, RuleMode> = {};
  /** The preview shown for an action's pending change, if any. */
  @state() private previews: Record<string, ModePreview> = {};

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private off() {
    this.data = null;
    this.dispatchEvent(new CustomEvent('capability-off', { bubbles: true }));
  }

  private async load() {
    try {
      this.data = await getAccessRules();
      this.draftModes = { ...this.data.modes };
      this.previews = {};
      this.yaml = await exportAccessRulesYaml().catch(() => '');
    } catch (error) {
      if (isCapabilityOff(error)) this.off();
      else
        this.error =
          error instanceof Error ? error.message : 'Could not load the rules';
    }
  }

  private async guard(action: () => Promise<void>) {
    this.error = '';
    try {
      await action();
    } catch (error) {
      if (isCapabilityOff(error)) this.off();
      else if (isConflict(error)) {
        // Someone else saved first: show their rules instead of ours.
        await this.load();
        this.error =
          'The rules changed while you were editing. They have been reloaded; make your change again.';
      } else this.error = error instanceof Error ? error.message : 'Failed';
    }
  }

  private value(id: string): string {
    return (
      this.renderRoot.querySelector<HTMLInputElement>(`#${id}`)?.value ?? ''
    ).trim();
  }

  private addRule = () => {
    const name = this.value('rule-name');
    const actions = this.value('rule-actions')
      .split(',')
      .map((a) => a.trim())
      .filter(Boolean);
    const subject = parseTags(this.value('rule-subject'));
    const resource = parseTags(this.value('rule-resource'));
    const errors = [...subject.errors, ...resource.errors];
    if (!name || actions.length === 0)
      errors.unshift('Name and actions are required.');
    if (errors.length) {
      this.error = errors.join(' ');
      return;
    }
    const rule: AccessRule = {
      name,
      effect: (this.value('rule-effect') || 'forbid') as AccessRule['effect'],
      actions,
      scope: (this.value('rule-scope') || 'self') as AccessRule['scope'],
      subject: { matchLabels: subject.tags },
      resource: {
        type: this.value('rule-resource-type') || undefined,
        matchLabels: resource.tags,
      },
    };
    void this.guard(async () => {
      this.data = await saveAccessRules(
        [...(this.data?.rules ?? []), rule],
        this.data?.version ?? null
      );
      this.yaml = await exportAccessRulesYaml().catch(() => this.yaml);
    });
  };

  private removeRule(index: number) {
    const rules = [...(this.data?.rules ?? [])];
    rules.splice(index, 1);
    void this.guard(async () => {
      this.data = await saveAccessRules(rules, this.data?.version ?? null);
      this.yaml = await exportAccessRulesYaml().catch(() => this.yaml);
    });
  }

  private applyYaml = () => {
    const text = this.value('rules-yaml');
    void this.guard(async () => {
      await applyAccessRulesYaml(text);
      await this.load();
    });
  };

  private runExplain = () => {
    const body = {
      subject: this.value('explain-subject'),
      action: this.value('explain-action'),
      resource: this.value('explain-resource'),
    };
    if (!body.subject || !body.action || !body.resource) {
      this.error = 'Explain needs a subject, an action and a resource.';
      return;
    }
    void this.guard(async () => {
      this.explain = await explainAccess(body);
    });
  };

  private chooseMode(action: string, mode: RuleMode) {
    this.draftModes = { ...this.draftModes, [action]: mode };
    const previews = { ...this.previews };
    delete previews[action];
    this.previews = previews;
  }

  /** Whether the pending change of an action may be saved yet. */
  canSaveMode(action: string): boolean {
    const draft = this.draftModes[action] ?? 'additive';
    const saved = this.data?.modes[action] ?? 'additive';
    if (draft === saved) return false;
    if (draft === 'require_permit') {
      return this.previews[action]?.mode === 'require_permit';
    }
    return true;
  }

  private preview(action: string) {
    void this.guard(async () => {
      const result = await previewMode(action, 'require_permit');
      this.previews = { ...this.previews, [action]: result };
    });
  }

  async saveMode(action: string) {
    if (!this.canSaveMode(action)) return;
    const mode = this.draftModes[action] ?? 'additive';
    await this.guard(async () => {
      await setMode(action, mode, this.previews[action]?.preview_token);
      await this.load();
    });
  }

  private renderRule(rule: AccessRule, index: number | null, from?: string) {
    return html`<tr data-inherited=${index === null ? 'true' : 'false'}>
      <td>
        ${rule.name}
        ${
          index === null
            ? html`<sl-badge variant="neutral" pill
                >Inherited from ${from || 'parent'}</sl-badge
              >`
            : nothing
        }
      </td>
      <td>${rule.effect}</td>
      <td>${rule.actions.join(', ')}</td>
      <td>${rule.scope ?? 'self'}</td>
      <td>
        ${
          index === null
            ? nothing
            : html`<sl-button
                size="small"
                @click=${() => this.removeRule(index)}
                >Remove</sl-button
              >`
        }
      </td>
    </tr>`;
  }

  private renderModes() {
    return html`<section data-testid="modes">
      <h3>Mode per action</h3>
      <p>
        Additive: forbid rules narrow access and permit rules do nothing.
        Require permit: an action is allowed only where a permit rule matches.
      </p>
      <div class="table-scroll">
        <table>
          ${MODE_ACTIONS.map((action) => {
            const preview = this.previews[action];
            const draft = this.draftModes[action] ?? 'additive';
            return html`<tr data-action=${action}>
              <td>${action}</td>
              <td>
                <sl-select
                  aria-label="Access rule value"
                  size="small"
                  .value=${draft}
                  @sl-change=${(e: Event) =>
                    this.chooseMode(
                      action,
                      (e.target as HTMLSelectElement).value as RuleMode
                    )}
                >
                  <sl-option value="additive">Additive</sl-option>
                  <sl-option value="require_permit">Require permit</sl-option>
                </sl-select>
                ${
                  draft === 'require_permit' &&
                  (this.data?.modes[action] ?? 'additive') !== 'require_permit'
                    ? html`<sl-button
                        size="small"
                        data-testid="mode-preview"
                        @click=${() => this.preview(action)}
                        >Preview who loses access</sl-button
                      >`
                    : nothing
                }
                ${
                  preview
                    ? html`<div
                        class="preview"
                        data-testid="mode-preview-result"
                      >
                        ${
                          preview.losing_access.length === 0
                            ? 'Nobody loses access.'
                            : html`These lose access:
                                <ul>
                                  ${preview.losing_access.map(
                                    (s) =>
                                      html`<li>
                                        ${s.kind}: ${s.name || s.id}
                                      </li>`
                                  )}
                                </ul>`
                        }
                      </div>`
                    : nothing
                }
              </td>
              <td>
                <sl-button
                  size="small"
                  variant="primary"
                  data-testid="mode-save"
                  ?disabled=${!this.canSaveMode(action)}
                  @click=${() => this.saveMode(action)}
                  >Save</sl-button
                >
              </td>
            </tr>`;
          })}
        </table>
      </div>
    </section>`;
  }

  render() {
    if (!this.data) {
      return this.error
        ? html`<p class="error" role="alert">${this.error}</p>`
        : nothing;
    }
    return html`
      ${this.error ? html`<p class="error" role="alert">${this.error}</p>` : nothing}
      <section>
        <h3>Access rules</h3>
        <div class="table-scroll">
          <table data-testid="rules">
            <thead>
              <tr>
                <th>Name</th>
                <th>Effect</th>
                <th>Actions</th>
                <th>Scope</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              ${this.data.rules.map((rule, i) => this.renderRule(rule, i))}
              ${this.data.inherited.map((rule) =>
                this.renderRule(rule, null, rule.account_name)
              )}
            </tbody>
          </table>
        </div>
        <div class="form">
          <sl-input id="rule-name" size="small" label="Name"></sl-input>
          <sl-select
            id="rule-effect"
            size="small"
            label="Effect"
            value="forbid"
          >
            <sl-option value="forbid">Forbid</sl-option>
            <sl-option value="permit">Permit</sl-option>
          </sl-select>
          <sl-input
            id="rule-actions"
            size="small"
            label="Actions"
            placeholder=${ALL_ACTIONS.slice(0, 2).join(', ')}
          ></sl-input>
          <sl-select id="rule-scope" size="small" label="Scope" value="self">
            <sl-option value="self">This account</sl-option>
            <sl-option value="subaccounts">Subaccounts</sl-option>
            <sl-option value="self_and_subaccounts"
              >This account and subaccounts</sl-option
            >
          </sl-select>
          <sl-input
            id="rule-subject"
            size="small"
            label="Subject tags"
            placeholder="site=north"
          ></sl-input>
          <sl-input
            id="rule-resource-type"
            size="small"
            label="Resource type"
            placeholder="ai_model"
          ></sl-input>
          <sl-input
            id="rule-resource"
            size="small"
            label="Resource tags"
            placeholder="tier=standard"
          ></sl-input>
          <sl-button size="small" variant="primary" @click=${this.addRule}
            >Add rule</sl-button
          >
        </div>
      </section>
      <section>
        <h3>YAML</h3>
        <sl-textarea
          aria-label="Access rules YAML"
          id="rules-yaml"
          rows="10"
          .value=${this.yaml}
        ></sl-textarea>
        <sl-button size="small" @click=${this.applyYaml}>Apply YAML</sl-button>
      </section>
      <section data-testid="explain">
        <h3>Explain</h3>
        <div class="row">
          <sl-input
            id="explain-subject"
            size="small"
            label="Subject"
            placeholder="agent:<id>"
          ></sl-input>
          <sl-input
            id="explain-action"
            size="small"
            label="Action"
            placeholder="tool:call"
          ></sl-input>
          <sl-input
            id="explain-resource"
            size="small"
            label="Resource"
            placeholder="tool:<id>"
          ></sl-input>
          <sl-button size="small" @click=${this.runExplain}>Explain</sl-button>
        </div>
        ${
          this.explain
            ? html`<p data-testid="explain-result">
                <strong
                  >${this.explain.effect === 'permit' ? 'Allowed' : 'Denied'}</strong
                >: ${this.explain.reason}
              </p>`
            : nothing
        }
      </section>
      ${this.renderModes()}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'access-rules-panel': AccessRulesPanel;
  }
}
