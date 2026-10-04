/**
 * Sensitive data tab of the Policies page.
 *
 * A plain form over the `sensitive_data` policy block: which data types to
 * look for, what to do when one is found, where to look, which tools keep
 * only references, a test box, a plain-language summary and the YAML it will
 * write. Saving hands the full policy document to the page, which runs it
 * through the same diff and import path as the YAML editor so versioning and
 * audit are unchanged.
 */
import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getSensitiveDataTypes,
  testSensitiveData,
  type SensitiveDataTestResponse,
  type SensitiveDataTypeInfo,
} from '../api';
import {
  unifiedYamlDiff,
  yamlDocumentsEqual,
} from '../utils/yaml-unified-diff';
import {
  ACTION_LABELS,
  ALL_TARGETS,
  TARGET_LABELS,
  blockToForm,
  blockYaml,
  emptyForm,
  formErrors,
  formToBlock,
  readSensitiveData,
  segments,
  storedForm,
  summarize,
  withSensitiveData,
  type ApproverView,
  type ScopeMode,
  type SensitiveDataForm,
  type SensitiveTarget,
  type TypeAction,
} from '../utils/sensitive-data-policy';

/** Server-side cap on the test endpoint's `text` field. */
export const TEST_TEXT_LIMIT = 20_000;

export interface AgentOption {
  id: string;
  name: string;
}

const NATIONAL_ID_LOCALES: Record<string, string> = {
  us: 'United States (SSN)',
  de: 'Germany (tax id)',
  uk: 'United Kingdom (NI number)',
  fr: 'France (NIR)',
  nl: 'Netherlands (BSN)',
};

const APPROVER_VIEWS: Record<ApproverView, string> = {
  redacted:
    'Approvers see the tool name, kept fields, key names and sizes. The raw input is never stored.',
  original_until_decided:
    'Approvers see the raw input in the console while the request is pending. It is kept encrypted and deleted when the request is decided.',
};

@customElement('sensitive-data-panel')
export class SensitiveDataPanel extends LitElement {
  /** The active policy as exported YAML. */
  @property({ type: String }) policyYaml = '';
  @property({ type: Array }) agents: AgentOption[] = [];
  @property({ type: Array }) tools: string[] = [];
  @property({ type: Array }) servers: string[] = [];
  @property({ type: Boolean }) saving = false;
  @property({ type: Boolean }) readonly = false;
  /** Set by the page when the agent or server pickers could not load. */
  @property({ type: String }) optionsError = '';

  @state() private _types: SensitiveDataTypeInfo[] = [];
  @state() private _typesError = '';
  @state() private _form: SensitiveDataForm = emptyForm();
  @state() private _original: unknown = undefined;
  @state() private _parseError = '';
  @state() private _dirty = false;
  @state() private _showErrors = false;
  @state() private _testText = '';
  @state() private _testing = false;
  @state() private _testError = '';
  @state() private _testResult: SensitiveDataTestResponse | null = null;
  @state() private _testedText = '';

  static styles = css`
    :host {
      display: block;
      color: var(--sl-color-neutral-900);
    }
    section {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium, 6px);
      padding: 1rem 1.25rem;
      margin-bottom: 1rem;
    }
    h3 {
      margin: 0 0 0.25rem;
      font-size: 1rem;
    }
    .hint {
      color: var(--sl-color-neutral-600);
      font-size: 0.875rem;
      margin: 0 0 0.75rem;
    }
    .type-row {
      display: grid;
      grid-template-columns: minmax(14rem, 1fr) auto;
      gap: 0.5rem 1rem;
      align-items: center;
      padding: 0.5rem 0;
      border-top: 1px solid var(--sl-color-neutral-100);
    }
    .type-row:first-of-type {
      border-top: none;
    }
    .type-label {
      font-weight: 600;
    }
    .type-desc {
      display: block;
      color: var(--sl-color-neutral-600);
      font-size: 0.85rem;
    }
    code,
    .example {
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.85rem;
    }
    fieldset.segmented {
      display: inline-flex;
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 999px;
      padding: 2px;
      margin: 0;
    }
    fieldset.segmented legend {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
    }
    fieldset.segmented label {
      padding: 0.2rem 0.75rem;
      border-radius: 999px;
      cursor: pointer;
      font-size: 0.85rem;
    }
    fieldset.segmented input {
      position: absolute;
      opacity: 0;
    }
    fieldset.segmented label:has(input:checked) {
      background: var(--sl-color-primary-600);
      color: var(--sl-color-neutral-0);
    }
    fieldset.segmented label:has(input:focus-visible) {
      outline: 2px solid var(--sl-color-primary-500);
      outline-offset: 1px;
    }
    .inline {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
    }
    .stack {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    .entry {
      border: 1px dashed var(--sl-color-neutral-300);
      border-radius: 6px;
      padding: 0.75rem;
      margin-bottom: 0.75rem;
    }
    input[type='text'],
    textarea,
    select {
      font: inherit;
      padding: 0.35rem 0.5rem;
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 4px;
      background: var(--sl-color-neutral-0);
      color: inherit;
    }
    select[multiple] {
      min-width: 16rem;
      min-height: 6rem;
    }
    textarea {
      width: 100%;
      box-sizing: border-box;
      min-height: 5rem;
    }
    input[aria-invalid='true'] {
      border-color: var(--sl-color-danger-600);
    }
    .error {
      color: var(--sl-color-danger-700);
      font-size: 0.85rem;
    }
    button {
      font: inherit;
      padding: 0.35rem 0.85rem;
      border-radius: 4px;
      border: 1px solid var(--sl-color-neutral-300);
      background: var(--sl-color-neutral-0);
      color: inherit;
      cursor: pointer;
    }
    button.primary {
      background: var(--sl-color-primary-600);
      border-color: var(--sl-color-primary-600);
      color: var(--sl-color-neutral-0);
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    mark {
      border-radius: 3px;
      padding: 0 2px;
      background: var(--sl-color-warning-200);
    }
    mark .tag {
      font-size: 0.7rem;
      margin-left: 0.25rem;
      color: var(--sl-color-neutral-700);
    }
    pre {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 4px;
      padding: 0.75rem;
      overflow: auto;
      white-space: pre-wrap;
      font-size: 0.8rem;
      margin: 0.25rem 0 0;
    }
    .actions {
      display: flex;
      gap: 0.5rem;
      justify-content: flex-end;
    }
  `;

  connectedCallback() {
    super.connectedCallback();
    void this._loadTypes();
  }

  willUpdate(changed: Map<string, unknown>) {
    if (changed.has('policyYaml') && !this._dirty) {
      this._loadFromPolicy();
    }
  }

  /** Reset the form to the active policy (after a save or on Revert). */
  reset() {
    this._dirty = false;
    this._showErrors = false;
    this._loadFromPolicy();
  }

  private _loadFromPolicy() {
    try {
      this._original = readSensitiveData(this.policyYaml);
      this._form = blockToForm(this._original);
      this._parseError = '';
    } catch (err: any) {
      this._parseError = err?.message || 'The active policy YAML is invalid.';
      this._original = undefined;
      this._form = emptyForm();
    }
  }

  private async _loadTypes() {
    try {
      const response = await getSensitiveDataTypes();
      this._types = response.types;
      this._typesError = '';
    } catch (err: any) {
      this._typesError = err?.message || 'Failed to load sensitive data types';
    }
  }

  private _update(mutate: (form: SensitiveDataForm) => void) {
    const next = structuredClone(this._form);
    mutate(next);
    this._form = next;
    this._dirty = true;
  }

  private get _block() {
    return formToBlock(this._form, this._original);
  }

  private get _errors() {
    return formErrors(this._form);
  }

  private get _labels(): Record<string, string> {
    const labels: Record<string, string> = {};
    for (const type of this._types) labels[type.id] = type.label;
    for (const agent of this.agents) labels[agent.id] = agent.name;
    return labels;
  }

  /** Server types plus custom names drafted here and not yet saved. */
  private get _allTypes(): SensitiveDataTypeInfo[] {
    const known = new Set(this._types.map((type) => type.id));
    const drafted = [
      ...this._form.customPatterns.map((item) => ({
        name: item.name.trim(),
        desc: 'Custom pattern',
        example: item.regex,
      })),
      ...this._form.keywords.map((item) => ({
        name: item.name.trim(),
        desc: 'Keyword list',
        example: item.terms.join(', '),
      })),
    ]
      .filter((item) => item.name && !known.has(item.name))
      .map((item) => ({
        id: item.name,
        label: item.name,
        description: item.desc,
        example: item.example,
        locales: [],
        checksum: false,
        builtin: false,
      }));
    return [...this._types, ...drafted];
  }

  private _proposedYaml(): string {
    return withSensitiveData(this.policyYaml, this._block);
  }

  private _save = () => {
    this._showErrors = true;
    if (Object.keys(this._errors).length) return;
    let yaml: string;
    try {
      yaml = this._proposedYaml();
    } catch (err: any) {
      this._parseError = err?.message || 'Could not build the policy YAML.';
      return;
    }
    this.dispatchEvent(
      new CustomEvent('sensitive-data-save', {
        detail: { yaml },
        bubbles: true,
        composed: true,
      })
    );
  };

  private _revert = () => this.reset();

  private _runTest = async () => {
    const text = this._testText;
    if (!text.trim()) {
      this._testError = 'Paste some sample text first.';
      this._testResult = null;
      return;
    }
    this._testing = true;
    this._testError = '';
    try {
      const types = Object.keys(this._form.types);
      const detectors = this._block?.detectors;
      this._testResult = await testSensitiveData({
        text,
        ...(types.length ? { types } : {}),
        ...(detectors ? { config: detectors } : {}),
      });
      this._testedText = text;
    } catch (err: any) {
      this._testResult = null;
      this._testError = err?.message || 'The test failed.';
    } finally {
      this._testing = false;
    }
  };

  render() {
    const errors = this._showErrors ? this._errors : {};
    return html`
      ${
        this._parseError
          ? html`<p class="error" role="alert">
              Could not read the active policy: ${this._parseError}
            </p>`
          : nothing
      }
      ${
        this.optionsError
          ? html`<p
              class="error"
              role="alert"
              data-testid="sensitive-options-error"
            >
              ${this.optionsError}
            </p>`
          : nothing
      }
      ${this._renderTypes()} ${this._renderCustom(errors)}
      ${this._renderWhere(errors)} ${this._renderReferences(errors)}
      ${this._renderTest()} ${this._renderSummary()} ${this._renderYaml()}
      <div class="actions">
        <button
          type="button"
          @click=${this._revert}
          ?disabled=${!this._dirty || this.saving}
        >
          Revert
        </button>
        <button
          type="button"
          class="primary"
          data-testid="sensitive-save"
          @click=${this._save}
          ?disabled=${this.readonly || this.saving || !this._dirty}
        >
          ${this.saving ? 'Saving...' : 'Review and save'}
        </button>
      </div>
      ${
        this._showErrors && Object.keys(this._errors).length
          ? html`<p class="error" role="alert">
              Fix the highlighted fields before saving.
            </p>`
          : nothing
      }
    `;
  }

  private _renderTypes() {
    const types = this._allTypes;
    return html`<section aria-labelledby="sd-types">
      <h3 id="sd-types">Data types</h3>
      <p class="hint">
        Pick what to look for and what happens when it is found. Detect only
        reports a match, Redact in logs stores
        <code>[REDACTED:type]</code> instead of the value, Block stops the call.
      </p>
      ${
        this._typesError
          ? html`<p class="error" role="alert">${this._typesError}</p>`
          : nothing
      }
      ${types.map((type) => this._renderTypeRow(type))}
      ${
        this._form.types['national_id']
          ? html`<fieldset class="inline" aria-describedby="sd-locale-hint">
              <legend>National id countries</legend>
              <span id="sd-locale-hint" class="hint"
                >None checked means every country.</span
              >
              ${Object.entries(NATIONAL_ID_LOCALES).map(
                ([code, label]) =>
                  html`<label
                    ><input
                      type="checkbox"
                      name="locale"
                      .value=${code}
                      .checked=${this._form.locales.includes(code)}
                      @change=${(e: Event) =>
                        this._update((form) => {
                          const on = (e.target as HTMLInputElement).checked;
                          form.locales = on
                            ? [...form.locales, code]
                            : form.locales.filter((item) => item !== code);
                        })}
                    />
                    ${label}</label
                  >`
              )}
            </fieldset>`
          : nothing
      }
    </section>`;
  }

  private _renderTypeRow(type: SensitiveDataTypeInfo) {
    const action = this._form.types[type.id];
    const id = `type-${type.id}`;
    return html`<div class="type-row" data-type=${type.id}>
      <label for=${id}>
        <input
          id=${id}
          type="checkbox"
          .checked=${!!action}
          @change=${(e: Event) =>
            this._update((form) => {
              if ((e.target as HTMLInputElement).checked) {
                form.types[type.id] = 'notify';
              } else {
                delete form.types[type.id];
              }
            })}
        />
        <span class="type-label">${type.label}</span>
        <span class="type-desc"
          >${type.description}${
            type.example
              ? html` Example: <span class="example">${type.example}</span>`
              : nothing
          }</span
        >
      </label>
      ${
        action
          ? html`<fieldset class="segmented">
              <legend>Action for ${type.label}</legend>
              ${(['notify', 'redact', 'deny'] as TypeAction[]).map(
                (value) =>
                  html`<label
                    ><input
                      type="radio"
                      name="action-${type.id}"
                      .value=${value}
                      .checked=${action === value}
                      @change=${() =>
                        this._update((form) => {
                          form.types[type.id] = value;
                        })}
                    />${ACTION_LABELS[value]}</label
                  >`
              )}
            </fieldset>`
          : html`<span></span>`
      }
    </div>`;
  }

  private _renderCustom(errors: Record<string, string>) {
    const nameError = (index: number, name: string) =>
      errors[`name-${index}-${name.trim()}`];
    const keywordOffset = this._form.customPatterns.length;
    return html`<section aria-labelledby="sd-custom">
      <h3 id="sd-custom">Custom patterns and keyword lists</h3>
      <p class="hint">
        Each one becomes a data type above. Regular expressions are checked by
        the server when you save.
      </p>
      ${this._form.customPatterns.map(
        (item, index) =>
          html`<div class="entry inline">
            <label
              >Name
              <input
                type="text"
                .value=${item.name}
                placeholder="employee_id"
                aria-invalid=${nameError(index, item.name) ? 'true' : 'false'}
                @input=${(e: Event) =>
                  this._update((form) => {
                    const value = (e.target as HTMLInputElement).value;
                    form.customPatterns[index].name = value;
                  })}
            /></label>
            <label
              >Regular expression
              <input
                type="text"
                .value=${item.regex}
                placeholder="EMP-\\d{6}"
                @input=${(e: Event) =>
                  this._update((form) => {
                    form.customPatterns[index].regex = (
                      e.target as HTMLInputElement
                    ).value;
                  })}
            /></label>
            <button
              type="button"
              aria-label="Remove pattern ${item.name || index + 1}"
              @click=${() =>
                this._update((form) => {
                  const [removed] = form.customPatterns.splice(index, 1);
                  delete form.types[removed.name.trim()];
                })}
            >
              Remove
            </button>
            ${
              nameError(index, item.name)
                ? html`<span class="error"
                    >${nameError(index, item.name)}</span
                  >`
                : nothing
            }
          </div>`
      )}
      ${this._form.keywords.map(
        (item, index) =>
          html`<div class="entry inline">
            <label
              >Name
              <input
                type="text"
                .value=${item.name}
                placeholder="project_codenames"
                aria-invalid=${
                  nameError(keywordOffset + index, item.name) ? 'true' : 'false'
                }
                @input=${(e: Event) =>
                  this._update((form) => {
                    const value = (e.target as HTMLInputElement).value;
                    form.keywords[index].name = value;
                  })}
            /></label>
            <label
              >Keywords (comma separated)
              <input
                type="text"
                .value=${item.terms.join(', ')}
                placeholder="Bluebird, Nightjar"
                @input=${(e: Event) =>
                  this._update((form) => {
                    form.keywords[index].terms = (
                      e.target as HTMLInputElement
                    ).value.split(',');
                  })}
            /></label>
            <button
              type="button"
              aria-label="Remove keyword list ${item.name || index + 1}"
              @click=${() =>
                this._update((form) => {
                  const [removed] = form.keywords.splice(index, 1);
                  delete form.types[removed.name.trim()];
                })}
            >
              Remove
            </button>
            ${
              nameError(keywordOffset + index, item.name)
                ? html`<span class="error"
                    >${nameError(keywordOffset + index, item.name)}</span
                  >`
                : nothing
            }
          </div>`
      )}
      <div class="inline">
        <button
          type="button"
          @click=${() =>
            this._update((form) => {
              form.customPatterns.push({ name: '', regex: '' });
            })}
        >
          Add pattern
        </button>
        <button
          type="button"
          @click=${() =>
            this._update((form) => {
              form.keywords.push({ name: '', terms: [] });
            })}
        >
          Add keyword list
        </button>
      </div>
    </section>`;
  }

  private _multiSelect(
    label: string,
    options: { value: string; label: string }[],
    selected: string[],
    onChange: (values: string[]) => void
  ) {
    return html`<label class="stack"
      >${label}
      <select
        multiple
        aria-label=${label}
        @change=${(e: Event) =>
          onChange(
            Array.from((e.target as HTMLSelectElement).selectedOptions).map(
              (option) => option.value
            )
          )}
      >
        ${[
          ...options,
          ...selected
            .filter((value) => !options.some((o) => o.value === value))
            .map((value) => ({ value, label: value })),
        ].map(
          (option) =>
            html`<option
              .value=${option.value}
              ?selected=${selected.includes(option.value)}
            >
              ${option.label}
            </option>`
        )}
      </select></label
    >`;
  }

  private get _agentOptions() {
    return this.agents.map((agent) => ({ value: agent.id, label: agent.name }));
  }

  private get _toolOptions() {
    return this.tools.map((tool) => ({ value: tool, label: tool }));
  }

  private get _serverOptions() {
    return this.servers.map((server) => ({ value: server, label: server }));
  }

  private _renderWhere(errors: Record<string, string>) {
    const modes: [ScopeMode, string][] = [
      ['all', 'All agents'],
      ['agents', 'Selected agents'],
      ['targets', 'Selected tools or servers'],
    ];
    return html`<section aria-labelledby="sd-where">
      <h3 id="sd-where">Where</h3>
      <fieldset class="inline">
        <legend>Apply to</legend>
        ${modes.map(
          ([value, label]) =>
            html`<label
              ><input
                type="radio"
                name="scope-mode"
                .value=${value}
                .checked=${this._form.scopeMode === value}
                @change=${() =>
                  this._update((form) => {
                    form.scopeMode = value;
                  })}
              />
              ${label}</label
            >`
        )}
      </fieldset>
      ${
        this._form.scopeMode === 'agents'
          ? this._multiSelect(
              'Agents',
              this._agentOptions,
              this._form.scope.agents,
              (values) =>
                this._update((form) => {
                  form.scope.agents = values;
                })
            )
          : nothing
      }
      ${
        this._form.scopeMode === 'targets'
          ? html`<div class="inline">
                ${this._multiSelect(
                  'Tools',
                  this._toolOptions,
                  this._form.scope.tools,
                  (values) =>
                    this._update((form) => {
                      form.scope.tools = values;
                    })
                )}
                ${this._multiSelect(
                  'MCP servers',
                  this._serverOptions,
                  this._form.scope.servers,
                  (values) =>
                    this._update((form) => {
                      form.scope.servers = values;
                    })
                )}
              </div>
              <p class="hint">
                Tool and server choices narrow tool inputs and results. Prompts
                and model replies are checked for every agent.
              </p>`
          : nothing
      }
      ${
        errors.scope
          ? html`<p class="error" role="alert">${errors.scope}</p>`
          : nothing
      }
      <fieldset class="inline">
        <legend>Check</legend>
        ${ALL_TARGETS.map(
          (target: SensitiveTarget) =>
            html`<label
              ><input
                type="checkbox"
                name="target"
                .value=${target}
                .checked=${this._form.on.includes(target)}
                @change=${(e: Event) =>
                  this._update((form) => {
                    const on = (e.target as HTMLInputElement).checked;
                    form.on = ALL_TARGETS.filter((item) =>
                      item === target ? on : form.on.includes(item)
                    );
                  })}
              />
              ${TARGET_LABELS[target]}</label
            >`
        )}
      </fieldset>
      ${errors.on ? html`<p class="error" role="alert">${errors.on}</p>` : nothing}
    </section>`;
  }

  private _renderReferences(errors: Record<string, string>) {
    return html`<section aria-labelledby="sd-refs">
      <h3 id="sd-refs">Reference-only logging</h3>
      <p class="hint">
        For the tools picked here no input or result is stored, only a
        fingerprint, key names, sizes and the fields you choose to keep.
      </p>
      ${this._form.referenceOnly.map((entry, index) =>
        this._renderReference(entry, index, errors)
      )}
      <button
        type="button"
        @click=${() =>
          this._update((form) => {
            form.referenceOnly.push({
              id: '',
              scope: { agents: [], tools: [], servers: [] },
              keepFields: [''],
              approverView: 'redacted',
            });
          })}
      >
        Add reference-only scope
      </button>
    </section>`;
  }

  private _renderReference(
    entry: SensitiveDataForm['referenceOnly'][number],
    index: number,
    errors: Record<string, string>
  ) {
    const set = (mutate: (item: typeof entry) => void) =>
      this._update((form) => mutate(form.referenceOnly[index]));
    return html`<div class="entry stack" data-ref=${index}>
      <div class="inline">
        <label
          >Name
          <input
            type="text"
            .value=${entry.id}
            placeholder="patient-tools"
            aria-invalid=${errors[`ref-${index}-id`] ? 'true' : 'false'}
            @input=${(e: Event) =>
              set((item) => {
                item.id = (e.target as HTMLInputElement).value;
              })}
        /></label>
        <button
          type="button"
          aria-label="Remove reference-only scope ${entry.id || index + 1}"
          @click=${() =>
            this._update((form) => {
              form.referenceOnly.splice(index, 1);
            })}
        >
          Remove
        </button>
      </div>
      ${
        errors[`ref-${index}-id`]
          ? html`<span class="error">${errors[`ref-${index}-id`]}</span>`
          : nothing
      }
      <div class="inline">
        ${this._multiSelect(
          'Tools',
          this._toolOptions,
          entry.scope.tools,
          (v) =>
            set((item) => {
              item.scope.tools = v;
            })
        )}
        ${this._multiSelect(
          'MCP servers',
          this._serverOptions,
          entry.scope.servers,
          (v) =>
            set((item) => {
              item.scope.servers = v;
            })
        )}
        ${this._multiSelect(
          'Agents',
          this._agentOptions,
          entry.scope.agents,
          (v) =>
            set((item) => {
              item.scope.agents = v;
            })
        )}
      </div>
      ${
        errors[`ref-${index}-scope`]
          ? html`<span class="error">${errors[`ref-${index}-scope`]}</span>`
          : nothing
      }
      <fieldset class="stack">
        <legend>Fields to keep as references</legend>
        <span class="hint"
          >JSON paths into the tool input, for example
          <code>$.consent_id</code> or <code>$.call.id</code>.</span
        >
        ${entry.keepFields.map((field, fieldIndex) => {
          const key = `ref-${index}-field-${fieldIndex}`;
          const invalid = field.trim() !== '' && !!formErrors(this._form)[key];
          return html`<div class="inline">
            <input
              type="text"
              aria-label="Field to keep ${fieldIndex + 1}"
              .value=${field}
              placeholder="$.consent_id"
              aria-invalid=${invalid ? 'true' : 'false'}
              aria-describedby=${invalid ? `${key}-error` : nothing}
              @input=${(e: Event) =>
                set((item) => {
                  item.keepFields[fieldIndex] = (
                    e.target as HTMLInputElement
                  ).value;
                })}
            />
            <button
              type="button"
              aria-label="Remove field ${fieldIndex + 1}"
              @click=${() =>
                set((item) => {
                  item.keepFields.splice(fieldIndex, 1);
                })}
            >
              Remove
            </button>
            ${
              invalid
                ? html`<span class="error" id="${key}-error"
                    >${formErrors(this._form)[key]}</span
                  >`
                : nothing
            }
          </div>`;
        })}
        <div>
          <button
            type="button"
            @click=${() =>
              set((item) => {
                item.keepFields.push('');
              })}
          >
            Add field
          </button>
        </div>
      </fieldset>
      <fieldset class="stack">
        <legend>What approvers see</legend>
        ${(Object.keys(APPROVER_VIEWS) as ApproverView[]).map(
          (view) =>
            html`<label
              ><input
                type="radio"
                name="approver-view-${index}"
                .value=${view}
                .checked=${entry.approverView === view}
                @change=${() =>
                  set((item) => {
                    item.approverView = view;
                  })}
              />
              <strong
                >${
                  view === 'redacted'
                    ? 'Redacted view'
                    : 'Original until decided'
                }</strong
              >: ${APPROVER_VIEWS[view]}</label
            >`
        )}
      </fieldset>
    </div>`;
  }

  private _renderTest() {
    const result = this._testResult;
    return html`<section aria-labelledby="sd-test">
      <h3 id="sd-test">Test it</h3>
      <label class="stack" for="sd-test-text"
        >Sample text (synthetic data only; it is not stored)</label
      >
      <textarea
        id="sd-test-text"
        maxlength=${TEST_TEXT_LIMIT}
        .value=${this._testText}
        placeholder="mail a@example.com"
        @input=${(e: Event) => {
          this._testText = (e.target as HTMLTextAreaElement).value;
        }}
      ></textarea>
      <div class="inline">
        <button
          type="button"
          data-testid="sensitive-test"
          @click=${this._runTest}
          ?disabled=${this._testing}
        >
          ${this._testing ? 'Testing...' : 'Test'}
        </button>
      </div>
      ${
        this._testError
          ? html`<p
              class="error"
              role="alert"
              data-testid="sensitive-test-error"
            >
              ${this._testError}
            </p>`
          : nothing
      }
      ${result ? this._renderTestResult(result) : nothing}
    </section>`;
  }

  private _renderTestResult(result: SensitiveDataTestResponse) {
    if (!result.matches.length) {
      return html`<p class="hint" data-testid="sensitive-test-empty">
        Nothing sensitive found in this text.
      </p>`;
    }
    const labels = this._labels;
    const stored = storedForm(
      this._testedText,
      result.matches,
      this._form.types
    );
    return html`<div data-testid="sensitive-test-result">
      <p>
        Found
        ${result.types_found
          .map(
            (type) =>
              `${labels[type] ?? type} (${result.matches.filter((m) => m.type === type).length})`
          )
          .join(', ')}.
      </p>
      <pre aria-label="Matches">
${segments(this._testedText, result.matches).map((part) =>
          part.type
            ? html`<mark data-type=${part.type}
                >${part.text}<span class="tag">${part.type}</span></mark
              >`
            : part.text
        )}</pre>
      <p><strong>Stored as</strong></p>
      <pre data-testid="sensitive-stored">
${
          stored.blocked
            ? 'The call is blocked, nothing is forwarded or stored.'
            : stored.text
        }</pre>
    </div>`;
  }

  private _renderSummary() {
    return html`<section aria-labelledby="sd-summary">
      <h3 id="sd-summary">Summary</h3>
      <p data-testid="sensitive-summary">
        ${summarize(this._form, this._labels)}
      </p>
    </section>`;
  }

  private _renderYaml() {
    let preview = '';
    let diff = '';
    try {
      const block = this._block;
      preview = blockYaml(block);
      const before = blockYaml(
        (this._original as Record<string, unknown>) ?? null
      );
      diff = yamlDocumentsEqual(before, preview)
        ? ''
        : unifiedYamlDiff(before, preview, 'sensitive_data');
    } catch {
      preview = '';
    }
    return html`<section aria-labelledby="sd-yaml">
      <h3 id="sd-yaml">Generated YAML</h3>
      <p class="hint">
        Only the <code>sensitive_data</code> block changes. Save shows the full
        policy diff before anything is applied.
      </p>
      <pre data-testid="sensitive-yaml" aria-label="Generated YAML">
${preview || '# No sensitive_data block'}</pre>
      ${
        diff
          ? html`<details>
              <summary>Changes against the active policy</summary>
              <pre data-testid="sensitive-diff">${diff}</pre>
            </details>`
          : nothing
      }
    </section>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'sensitive-data-panel': SensitiveDataPanel;
  }
}
