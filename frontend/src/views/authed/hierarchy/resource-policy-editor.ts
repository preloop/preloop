import { randomUuid } from '../../../utils/random-uuid';
import { LitElement, html, css, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getResourceAccess,
  emptyRule,
  ruleDefinition,
  previewResourceAccess,
  putResourceAccess,
  explainResourceAccess,
  SUBJECT_DIRECTORY_LIMIT,
  type Action,
  type PolicyRule,
  type ResourceSummary,
  type ResourceUpdate,
  type ResourcePreview,
  type ResourceExplanation,
  type Subject,
  type SubjectOption,
} from '../../../access-policy-api';
import { isConflict } from '../../../hierarchy-api';
import { describeSubject } from './access-rule-form';
import './access-rule-form';

@customElement('resource-policy-editor')
export class ResourcePolicyEditor extends LitElement {
  static styles = css`
    :host {
      display: block;
      min-width: 0;
    }
    h3 {
      font-size: 1em;
      margin: 1rem 0 0.5rem;
    }
    p {
      margin: 0.5rem 0;
    }
    .muted {
      color: var(--sl-color-neutral-600);
    }
    .error {
      color: var(--sl-color-danger-700);
    }
    label {
      display: grid;
      gap: 0.3rem;
      margin: 0.5rem 0;
    }
    input,
    select,
    button {
      font: inherit;
      max-width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.3rem;
      padding: 0.4rem;
      color: inherit;
      background: var(--sl-color-neutral-0);
    }
    button {
      cursor: pointer;
    }
    .actions {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin: 0.75rem 0;
    }
    .rule {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 0.4rem;
      padding: 0.65rem;
      margin: 0.5rem 0;
      overflow-wrap: anywhere;
    }
    .preview {
      background: var(--sl-color-warning-50);
      padding: 0.65rem;
      overflow-wrap: anywhere;
    }
    ul {
      padding-inline-start: 1.2rem;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .binding {
      display: flex;
      align-items: center;
    }
    a {
      color: var(--sl-color-primary-700);
    }
  `;
  @property({ attribute: false }) summary!: ResourceSummary;
  @property({ attribute: false }) subjects: SubjectOption[] = [];
  @property({ type: Boolean }) readOnly = false;
  @property({ type: Boolean }) disabled = false;
  @state() private draft: ResourceUpdate = { rules: [], requirements: [] };
  @state() private edit: PolicyRule | null = null;
  @state() private preview: ResourcePreview | null = null;
  @state() private busy = false;
  @state() private error = '';
  @state() private explanation: ResourceExplanation | null = null;
  @state() private subjectKind: Subject['kind'] = 'user';
  @state() private subjectId = '';
  @state() private action: Action = 'resource:view';
  @state() private flowId = '';
  private requestId = 0;
  private keepDraft = false;
  focus(options?: FocusOptions): void {
    this.renderRoot
      .querySelector<HTMLElement>(
        'select:not(:disabled),button:not(:disabled),input:not(:disabled)'
      )
      ?.focus(options);
  }
  private get canManage(): boolean {
    return this.summary.can_manage && !this.readOnly;
  }
  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('summary') && this.summary) {
      if (this.keepDraft) {
        this.keepDraft = false;
        this.preview = null;
        this.error =
          'Current version reloaded. Your draft is kept; review and preview it again.';
      } else this.reset();
      this.action = this.summary.supported_actions[0];
      this.explanation = null;
    }
  }
  protected updated(changed: PropertyValues): void {
    if (changed.has('busy'))
      this.dispatchEvent(
        new CustomEvent('resource-busy', {
          detail: this.busy,
          bubbles: true,
          composed: true,
        })
      );
  }
  disconnectedCallback(): void {
    ++this.requestId;
    super.disconnectedCallback();
  }
  private reset(): void {
    ++this.requestId;
    this.busy = false;
    this.draft = {
      rules: this.summary.document.rules.map(ruleDefinition),
      requirements: structuredClone(this.summary.document.requirements),
    };
    this.preview = null;
    this.edit = null;
    this.error = '';
  }
  private changed(): void {
    this.preview = null;
    this.error = '';
    ++this.requestId;
  }
  private mode(action: Action, restricted: boolean): void {
    this.changed();
    this.draft = {
      ...this.draft,
      requirements: [
        ...this.draft.requirements.filter((r) => r.action !== action),
        ...(restricted
          ? [
              {
                action,
                mode: 'require_permit' as const,
                permit_rule_ids: this.draft.rules
                  .filter((r) => r.is_enabled && r.actions.includes(action))
                  .map((r) => String(r.id)),
              },
            ]
          : []),
      ],
    };
  }
  private bind(action: Action, id: string, checked: boolean): void {
    this.changed();
    this.draft = {
      ...this.draft,
      requirements: this.draft.requirements.map((r) =>
        r.action === action
          ? {
              ...r,
              permit_rule_ids: checked
                ? [...r.permit_rule_ids, id]
                : r.permit_rule_ids.filter((value) => value !== id),
            }
          : r
      ),
    };
  }
  private useRule(event: CustomEvent<PolicyRule>): void {
    event.stopPropagation();
    this.changed();
    const value = ruleDefinition(event.detail);
    value.id = this.edit?.id || randomUuid();
    const existed = this.draft.rules.some((r) => r.id === value.id);
    this.draft = {
      rules: [...this.draft.rules.filter((r) => r.id !== value.id), value],
      requirements: this.draft.requirements.map((r) => ({
        ...r,
        permit_rule_ids: [
          ...r.permit_rule_ids.filter(
            (id) => id !== value.id || value.actions.includes(r.action)
          ),
          ...(!existed && value.actions.includes(r.action)
            ? [String(value.id)]
            : []),
        ],
      })),
    };
    this.edit = null;
  }
  private removePermit(id: string): void {
    this.changed();
    this.draft = {
      rules: this.draft.rules.filter((r) => r.id !== id),
      requirements: this.draft.requirements.map((r) => ({
        ...r,
        permit_rule_ids: r.permit_rule_ids.filter((value) => value !== id),
      })),
    };
  }
  private report(error: unknown): void {
    this.error = isConflict(error)
      ? 'Policy, resource or preview changed or expired. Your draft is kept. Reload the current version, then preview again.'
      : `${error instanceof Error ? error.message : 'Request failed'}. Your draft is kept.`;
  }
  private async previewChanges(): Promise<void> {
    if (!this.canManage || this.disabled || this.busy || this.edit) return;
    const request = ++this.requestId;
    this.busy = true;
    this.error = '';
    this.preview = null;
    try {
      const value = await previewResourceAccess(
        this.summary.document.resource_type,
        this.summary.document.resource_id,
        this.draft,
        this.summary.document.version
      );
      if (request === this.requestId) this.preview = value;
    } catch (error) {
      if (request === this.requestId) this.report(error);
    } finally {
      if (request === this.requestId) this.busy = false;
    }
  }
  private async save(): Promise<void> {
    if (
      !this.canManage ||
      this.disabled ||
      this.busy ||
      !this.preview ||
      this.edit
    )
      return;
    if (Date.parse(this.preview.expires_at) <= Date.now()) {
      this.preview = null;
      this.error = 'Preview expired. Preview your draft again.';
      return;
    }
    const request = ++this.requestId;
    this.busy = true;
    this.error = '';
    try {
      const value = await putResourceAccess(
        this.summary.document.resource_type,
        this.summary.document.resource_id,
        this.draft,
        this.summary.document.version,
        this.preview.preview_token
      );
      if (request !== this.requestId) return;
      this.dispatchEvent(
        new CustomEvent<ResourceSummary>('resource-summary', {
          detail: value,
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      if (request === this.requestId) {
        this.preview = null;
        this.report(error);
      }
    } finally {
      if (request === this.requestId) this.busy = false;
    }
  }
  private async reload(): Promise<void> {
    if (this.busy || this.disabled) return;
    const request = ++this.requestId;
    this.busy = true;
    try {
      const value = await getResourceAccess(
        this.summary.document.resource_type,
        this.summary.document.resource_id
      );
      if (request !== this.requestId) return;
      this.keepDraft = true;
      this.summary = value;
      this.dispatchEvent(
        new CustomEvent('resource-reloaded', {
          detail: value,
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      if (request === this.requestId) this.report(error);
    } finally {
      if (request === this.requestId) this.busy = false;
    }
  }
  private async explain(): Promise<void> {
    if (this.busy || this.disabled) return;
    if (!this.subjectId) {
      this.error = 'Choose a user or enter a principal UUID.';
      return;
    }
    const request = ++this.requestId;
    this.busy = true;
    this.error = '';
    this.explanation = null;
    try {
      const value = await explainResourceAccess(
        this.summary.document.resource_type,
        this.summary.document.resource_id,
        { kind: this.subjectKind, id: this.subjectId },
        this.action,
        this.flowId || undefined
      );
      if (request === this.requestId) this.explanation = value;
    } catch (error) {
      if (request === this.requestId)
        this.error =
          error instanceof Error ? error.message : 'Could not explain access';
    } finally {
      if (request === this.requestId) this.busy = false;
    }
  }
  render() {
    if (!this.summary) return nothing;
    const doc = this.summary.document;
    return html`<section aria-labelledby="people-heading">
        <h3 id="people-heading">People & teams</h3>
        <p class="muted">
          Account defaults inherit the account's policy. Restricted adds an
          independent gate for this resource. RBAC, inherited requirements and
          forbid rules still cap access.
        </p>
        ${!this.canManage ? html`<p>Read-only policy from account ${doc.account_id}.</p>` : nothing}
        ${this.summary.supported_actions.map(
          (action) =>
            html`<label
                >${action}<select
                  data-mode=${action}
                  .value=${this.draft.requirements.some((r) => r.action === action) ? 'restricted' : 'inherit'}
                  ?disabled=${!this.canManage || this.busy || this.disabled}
                  @change=${(e: Event) => this.mode(action, (e.target as HTMLSelectElement).value === 'restricted')}
                >
                  <option value="inherit">Inherit account defaults</option>
                  <option value="restricted">
                    Restricted: require designated local permit
                  </option>
                </select></label
              >
              ${this.draft.requirements.some((r) => r.action === action && !r.permit_rule_ids.length) ? html`<p class="error">${action}: no designated permits; this gate denies all subjects.</p>` : nothing}`
        )}
        <p class="muted">
          Only the actions above are supported for this resource. Generic edit
          or admin access is unavailable here.
        </p>
        ${this.draft.rules.map(
          (rule) =>
            html`<div class="rule" data-local-rule=${rule.id ?? ''}>
              <strong>${rule.name}</strong>
              <p>${describeSubject(rule, this.subjects)}</p>
              <p>${rule.actions.join(', ')} · local permit</p>
              ${this.draft.requirements.filter((r) => rule.actions.includes(r.action)).map((r) => html`<label class="binding"><input type="checkbox" .checked=${r.permit_rule_ids.includes(String(rule.id))} ?disabled=${!this.canManage || this.busy || this.disabled} @change=${(e: Event) => this.bind(r.action, String(rule.id), (e.target as HTMLInputElement).checked)} />Designated for ${r.action}</label>`)}
              ${this.canManage ? html`<div class="actions"><button ?disabled=${this.busy || this.disabled} @click=${() => (this.edit = rule)}>Edit permit</button><button ?disabled=${this.busy || this.disabled} @click=${() => this.removePermit(String(rule.id))}>Remove permit</button></div>` : nothing}
            </div>`
        )}
        ${this.canManage && !this.edit ? html`<button data-testid="add-local-permit" ?disabled=${this.busy || this.disabled} @click=${() => (this.edit = { ...emptyRule(), name: 'Resource permit', actions: [this.summary.supported_actions[0]] })}>Add people or team permit</button>` : nothing}
        ${
          this.edit
            ? html`<access-rule-form
                .rule=${this.edit}
                .subjects=${this.subjects}
                .actions=${this.summary.supported_actions}
                .fixedResource=${{ type: doc.resource_type, id: doc.resource_id }}
                .busy=${this.busy || this.disabled}
                @rule-submit=${this.useRule}
                @rule-cancel=${(event: Event) => {
                  event.stopPropagation();
                  this.edit = null;
                }}
              ></access-rule-form>`
            : nothing
        }
        ${
          this.preview
            ? html`<div
                class="preview"
                data-testid="resource-preview"
                role="status"
              >
                <strong>Policy-only preview</strong>
                <p>
                  Runtime context is unknown. This is not final effective
                  access.
                </p>
                ${
                  this.preview.changes.length
                    ? html`<ul>
                        ${this.preview.changes.map((change) => html`<li>${change.kind}: ${change.id} · ${change.action}: ${change.before} → ${change.after}${change.flow_id ? ` · flow ${change.flow_id}` : ''}</li>`)}
                      </ul>`
                    : html`<p>
                        No policy decisions change for known subjects.
                      </p>`
                }
                <p>Expires ${this.preview.expires_at}</p>
              </div>`
            : nothing
        }
        ${this.error ? html`<p class="error" role="alert">${this.error}</p>` : nothing}
        ${
          this.canManage
            ? html`<div class="actions">
                  <button
                    data-testid="resource-preview-button"
                    ?disabled=${this.disabled || this.busy || !!this.edit}
                    @click=${this.previewChanges}
                  >
                    Preview changes</button
                  ><button
                    data-testid="resource-save"
                    ?disabled=${this.disabled || this.busy || !!this.edit || !this.preview}
                    @click=${this.save}
                  >
                    Save access</button
                  ><button
                    data-testid="resource-cancel"
                    ?disabled=${this.busy || this.disabled}
                    @click=${() => {
                      this.reset();
                      this.dispatchEvent(
                        new CustomEvent('access-close', {
                          bubbles: true,
                          composed: true,
                        })
                      );
                    }}
                  >
                    Cancel
                  </button>
                </div>
                ${this.error ? html`<button data-testid="resource-reload" ?disabled=${this.busy || this.disabled} @click=${this.reload}>Reload current version, keep draft</button>` : nothing}`
            : nothing
        }
      </section>
      <section>
        <h3>Applied rules</h3>
        <p class="muted">
          Matching local and inherited rules remain subject to their conditions
          and independent account gates. Account-wide rules are additive permits
          or denial ceilings, not local restrictions.
        </p>
        ${this.summary.inherited_policies.map((policy) => html`<p>Inherited from account ${policy.account_id}: ${policy.requirements.map((r) => r.action).join(', ') || 'inherit'} (read-only)</p>`)}
        ${this.summary.applied_rules.map(
          (rule) =>
            html`<details>
              <summary>
                ${rule.name} · ${rule.effect} ·
                ${rule.inherited ? 'Inherited' : 'Account policy'} from
                ${rule.account_id}
              </summary>
              <p>
                ${rule.actions.join(', ')} ·
                ${describeSubject(rule, this.subjects)}
              </p>
              <pre>${JSON.stringify(rule.conditions, null, 2)}</pre>
              <p>Read-only here; edit at the policy source.</p>
            </details>`
        )}
        ${!this.summary.applied_rules.length && !this.summary.inherited_policies.length ? html`<p>No additional matching rule is reported. Account defaults and RBAC still apply.</p>` : nothing}
        <p>
          <a
            data-testid="add-resource-rule"
            href=${`/console/policies?tab=access-rules&resource_type=${encodeURIComponent(doc.resource_type)}&resource_id=${encodeURIComponent(doc.resource_id)}`}
            >${this.canManage ? 'Add rule in Policies → Access rules' : 'View Policies → Access rules'}</a
          >
        </p>
      </section>
      <section>
        <h3>Explain access</h3>
        <p class="muted">
          Explains persisted policy, including known RBAC denials. Unsaved edits
          are evaluated by Preview.
        </p>
        <label
          >User<select
            data-testid="explain-user"
            .value=${this.subjectKind === 'user' ? this.subjectId : ''}
            ?disabled=${this.busy || this.disabled}
            @change=${(e: Event) => {
              this.subjectKind = 'user';
              this.subjectId = (e.target as HTMLSelectElement).value;
              this.explanation = null;
            }}
          >
            <option value="">Choose a user or specify a principal below</option>
            ${this.subjects.filter((s) => s.kind === 'user').map((s) => html`<option value=${s.id}>${s.label}</option>`)}
          </select></label
        >
        <p class="muted">
          The name list includes up to ${SUBJECT_DIRECTORY_LIMIT} users. Enter a
          principal UUID below for anyone else.
        </p>
        <label
          >Principal kind<select
            .value=${this.subjectKind}
            ?disabled=${this.busy || this.disabled}
            @change=${(e: Event) => {
              this.subjectKind = (e.target as HTMLSelectElement)
                .value as Subject['kind'];
              this.explanation = null;
            }}
          >
            ${['user', 'agent', 'api_key', 'flow'].map((kind) => html`<option value=${kind}>${kind}</option>`)}
          </select></label
        >
        <label
          >Principal UUID<input
            data-testid="explain-id"
            .value=${this.subjectId}
            ?disabled=${this.busy || this.disabled}
            @input=${(e: Event) => {
              this.subjectId = (e.target as HTMLInputElement).value;
              this.explanation = null;
            }}
        /></label>
        <label
          >Action<select
            data-testid="explain-action"
            .value=${this.action}
            ?disabled=${this.busy || this.disabled}
            @change=${(e: Event) => {
              this.action = (e.target as HTMLSelectElement).value as Action;
              this.explanation = null;
            }}
          >
            ${this.summary.supported_actions.map((action) => html`<option value=${action}>${action}</option>`)}
          </select></label
        >
        ${
          this.action === 'runner:accept'
            ? html`<label
                >Flow UUID (optional runtime context)<input
                  .value=${this.flowId}
                  ?disabled=${this.busy || this.disabled}
                  @input=${(e: Event) => {
                    this.flowId = (e.target as HTMLInputElement).value;
                    this.explanation = null;
                  }}
              /></label>`
            : nothing
        }
        <button
          data-testid="resource-explain"
          ?disabled=${this.busy || this.disabled}
          @click=${this.explain}
        >
          Explain
        </button>
        ${
          this.explanation
            ? html`<div role="status" data-testid="resource-explanation">
                <strong
                  >${this.explanation.decision === 'deny' ? 'Denied' : 'Conditional — runtime checks required'}</strong
                >
                <p>${this.explanation.reason}</p>
                <p>
                  Policy: ${this.explanation.policy_decision}; RBAC:
                  ${this.explanation.rbac}; runtime context: unknown.
                </p>
                <p>
                  Unverified: ${this.explanation.unverified_ceilings.join(', ')}
                </p>
                <p>Rules: ${this.explanation.rule_ids.join(', ') || 'none'}</p>
              </div>`
            : nothing
        }
      </section>`;
  }
}
declare global {
  interface HTMLElementTagNameMap {
    'resource-policy-editor': ResourcePolicyEditor;
  }
}
