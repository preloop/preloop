import { LitElement, html, css, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import type {
  AnswerFieldError,
  QuestionField,
  QuestionItem,
  QuestionSchema,
} from '../types';

/** What the form currently holds, and whether it may be submitted. */
export interface AnswerFormState {
  answer: Record<string, unknown>;
  valid: boolean;
}

/** Radios up to this many choices; a select above it. */
const RADIO_LIMIT = 4;

/**
 * The answer to a structured question, as a form.
 *
 * A question can carry `items` (the rows it is about) and an `input_schema`
 * (the shape of the answer). This renders that pair: a table of items with a
 * checkbox and per-row fields, switches for booleans, radios or a select for
 * enums, inputs for text and numbers. The answer leaves as JSON that already
 * fits the schema, which is the whole point: the previous version of this
 * flow asked a human to type a JSON array into a textarea, and what came back
 * was prose.
 *
 * Validation here is a courtesy to the person filling the form. The server
 * validates the same schema again and is the one that decides.
 *
 * Events:
 *  - `answer-change` (detail: AnswerFormState) on every edit.
 */
@customElement('answer-form')
export class AnswerForm extends LitElement {
  /** The shape of the answer. Nothing renders without it. */
  @property({ type: Object })
  schema: QuestionSchema | null = null;

  /** The rows the question is about. May be empty. */
  @property({ type: Array })
  items: QuestionItem[] = [];

  /** Disables every control while a decision is in flight. */
  @property({ type: Boolean })
  disabled = false;

  /**
   * Who the platform will record as the author of this decision. Shown in
   * `x-autofill: author` fields, which are never editable.
   */
  @property({ type: String })
  author = '';

  /** Tighter spacing for inline use inside a list card. */
  @property({ type: Boolean, reflect: true })
  compact = false;

  @state()
  private value: Record<string, unknown> = {};

  /** Field path -> message. Shown once the operator tries to submit. */
  @state()
  private errors: Record<string, string> = {};

  @state()
  private showErrors = false;

  static styles = css`
    :host {
      display: block;
    }

    .field {
      margin-bottom: 1rem;
    }

    :host([compact]) .field {
      margin-bottom: 0.75rem;
    }

    .field:last-child {
      margin-bottom: 0;
    }

    .field-label {
      display: block;
      font-size: var(--console-text-body, 14px);
      font-weight: 600;
      color: var(--sl-color-neutral-900);
      margin-bottom: 0.25rem;
    }

    .required-marker {
      color: var(--sl-color-danger-600);
      margin-left: 0.125rem;
    }

    .field-help {
      font-size: var(--console-text-meta, 13px);
      color: var(--sl-color-neutral-600);
      margin: 0 0 0.375rem 0;
    }

    .field-error {
      font-size: var(--console-text-meta, 13px);
      color: var(--sl-color-danger-700);
      margin: 0.25rem 0 0 0;
    }

    .group {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 4px;
      padding: 0.75rem;
      margin: 0;
    }

    .item-table {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--console-text-body, 14px);
    }

    .item-table th {
      text-align: left;
      font-size: var(--console-text-meta, 13px);
      font-weight: 600;
      color: var(--sl-color-neutral-600);
      padding: 0.25rem 0.5rem 0.5rem 0;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }

    .item-table td {
      vertical-align: top;
      padding: 0.5rem 0.5rem 0.5rem 0;
      border-bottom: 1px solid var(--sl-color-neutral-100);
    }

    .item-table td:last-child,
    .item-table th:last-child {
      padding-right: 0;
    }

    .item-title {
      font-weight: 500;
      color: var(--sl-color-neutral-900);
    }

    .item-detail {
      font-size: var(--console-text-meta, 13px);
      color: var(--sl-color-neutral-600);
      margin-top: 0.125rem;
    }

    .item-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem;
      margin-top: 0.25rem;
    }

    .chip::part(base) {
      font-size: 12px;
      font-weight: 500;
      padding: 2px 8px;
      border: none;
    }

    .row-fields {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }

    .row-disabled-hint {
      font-size: var(--console-text-meta, 13px);
      color: var(--sl-color-neutral-500);
    }

    .autofilled {
      display: flex;
      align-items: center;
      gap: 0.375rem;
      font-size: var(--console-text-body, 14px);
      color: var(--sl-color-neutral-700);
      background: var(--sl-color-neutral-100);
      border-radius: 4px;
      padding: 0.375rem 0.5rem;
    }

    .autofilled sl-icon {
      color: var(--sl-color-neutral-500);
    }

    /* A phone gets the same table as one row per finding, stacked. */
    @media (max-width: 520px) {
      .item-table,
      .item-table tbody,
      .item-table tr,
      .item-table td {
        display: block;
        width: 100%;
      }

      .item-table thead {
        display: none;
      }

      .item-table tr {
        border-bottom: 1px solid var(--sl-color-neutral-200);
        padding: 0.5rem 0;
      }

      .item-table td {
        border-bottom: none;
        padding: 0.25rem 0;
      }
    }
  `;

  /** The schema this form was last built for, so a re-render keeps the input. */
  private builtFor: QuestionSchema | null = null;

  /**
   * Defaults are computed before the first paint, and again only when the
   * question itself changes. Doing it after an update would throw away what
   * the operator has typed on every re-render of the parent.
   */
  willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('schema') || this.builtFor !== this.schema) {
      this.builtFor = this.schema;
      this.resetToDefaults();
    }
  }

  /** The answer as it stands, with empty fields dropped. */
  get answer(): Record<string, unknown> {
    return this.cleanedAnswer();
  }

  /**
   * Validate and show the results. Returns true when the form may be sent.
   *
   * Called by whatever owns the submit button, so an incomplete waiver never
   * becomes a decision nobody can explain later.
   */
  validate(): boolean {
    this.errors = this.collectErrors();
    this.showErrors = true;
    return Object.keys(this.errors).length === 0;
  }

  /** Put the server's 422 complaints on the fields that earned them. */
  setServerErrors(errors: AnswerFieldError[]): void {
    const mapped: Record<string, string> = {};
    for (const error of errors || []) {
      mapped[error.path || ''] = error.message;
    }
    this.errors = mapped;
    this.showErrors = true;
  }

  private get properties(): Record<string, QuestionField> {
    return this.schema?.properties ?? {};
  }

  private get requiredNames(): string[] {
    return this.schema?.required ?? [];
  }

  private resetToDefaults(): void {
    const next: Record<string, unknown> = {};
    for (const [name, field] of Object.entries(this.properties)) {
      if (field['x-autofill']) continue;
      if (field.default !== undefined && field.default !== null) {
        next[name] = field.default;
      } else if (field.type === 'boolean') {
        next[name] = false;
      } else if (field.type === 'array') {
        next[name] = [];
      } else if (field.type === 'object') {
        next[name] = {};
      }
    }
    this.value = next;
    this.errors = {};
    this.showErrors = false;
  }

  /** Rows of an array-of-objects field, always as a list. */
  private rowsOf(name: string): Array<Record<string, unknown>> {
    const current = this.value[name];
    return Array.isArray(current)
      ? (current as Array<Record<string, unknown>>)
      : [];
  }

  private idsOf(name: string): string[] {
    const current = this.value[name];
    return Array.isArray(current) ? (current as string[]) : [];
  }

  private setField(name: string, next: unknown): void {
    this.value = { ...this.value, [name]: next };
    // Editing a field is an answer to the complaint about it.
    if (this.errors[name]) {
      const { [name]: _dropped, ...rest } = this.errors;
      this.errors = rest;
    }
    this.emitChange();
  }

  private emitChange(): void {
    this.dispatchEvent(
      new CustomEvent<AnswerFormState>('answer-change', {
        detail: {
          answer: this.cleanedAnswer(),
          valid: Object.keys(this.collectErrors()).length === 0,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  /**
   * The answer we would send: blank optional fields removed, so an untouched
   * text box is an unanswered question rather than an empty string on record.
   */
  private cleanedAnswer(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [name, field] of Object.entries(this.properties)) {
      if (field['x-autofill']) continue;
      const raw = this.value[name];
      if (raw === undefined || raw === null) continue;
      if (typeof raw === 'string' && raw.trim() === '') continue;
      if (Array.isArray(raw)) {
        if (raw.length === 0) continue;
        out[name] = raw;
        continue;
      }
      if (typeof raw === 'object') {
        const group = this.cleanedGroup(field, raw as Record<string, unknown>);
        if (Object.keys(group).length > 0) out[name] = group;
        continue;
      }
      out[name] = raw;
    }
    return out;
  }

  private cleanedGroup(
    field: QuestionField,
    raw: Record<string, unknown>
  ): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const [name, spec] of Object.entries(field.properties ?? {})) {
      if (spec['x-autofill']) continue;
      const entry = raw[name];
      if (entry === undefined || entry === null) continue;
      if (typeof entry === 'string' && entry.trim() === '') continue;
      out[name] = entry;
    }
    return out;
  }

  /** The same rules the server applies, so the two rarely disagree. */
  private collectErrors(): Record<string, string> {
    const errors: Record<string, string> = {};
    if (!this.schema) return errors;
    const required = new Set(this.requiredNames);

    for (const [name, field] of Object.entries(this.properties)) {
      if (field['x-autofill']) continue;
      const raw = this.value[name];
      const empty =
        raw === undefined ||
        raw === null ||
        (typeof raw === 'string' && raw.trim() === '') ||
        (Array.isArray(raw) && raw.length === 0);

      if (empty) {
        if (required.has(name)) {
          errors[name] = `${this.labelFor(name, field)} is required`;
        }
        continue;
      }

      if (field.type === 'array') {
        if (field.minItems && (raw as unknown[]).length < field.minItems) {
          errors[name] = `Pick at least ${field.minItems}`;
        }
        if (field.maxItems && (raw as unknown[]).length > field.maxItems) {
          errors[name] = `Pick at most ${field.maxItems}`;
        }
        const rowSpec = field.items;
        if (rowSpec?.type === 'object') {
          const rowRequired = new Set(rowSpec.required ?? []);
          this.rowsOf(name).forEach((row, index) => {
            for (const [key, spec] of Object.entries(
              rowSpec.properties ?? {}
            )) {
              if (spec['x-autofill']) continue;
              const entry = row[key];
              const missing =
                entry === undefined ||
                entry === null ||
                (typeof entry === 'string' && entry.trim() === '');
              const path = `${name}[${index}].${key}`;
              if (missing) {
                if (rowRequired.has(key)) {
                  errors[path] = `${this.labelFor(key, spec)} is required`;
                }
                continue;
              }
              const message = this.scalarError(spec, entry);
              if (message) errors[path] = message;
            }
          });
        }
        continue;
      }

      if (field.type === 'object') {
        const group = (raw as Record<string, unknown>) ?? {};
        const groupRequired = new Set(field.required ?? []);
        for (const [key, spec] of Object.entries(field.properties ?? {})) {
          if (spec['x-autofill']) continue;
          const entry = group[key];
          const missing =
            entry === undefined ||
            entry === null ||
            (typeof entry === 'string' && entry.trim() === '');
          const path = `${name}.${key}`;
          if (missing) {
            if (groupRequired.has(key)) {
              errors[path] = `${this.labelFor(key, spec)} is required`;
            }
            continue;
          }
          const message = this.scalarError(spec, entry);
          if (message) errors[path] = message;
        }
        continue;
      }

      const message = this.scalarError(field, raw);
      if (message) errors[name] = message;
    }
    return errors;
  }

  private scalarError(field: QuestionField, value: unknown): string | null {
    if (field.type === 'number' || field.type === 'integer') {
      const numeric = typeof value === 'number' ? value : Number(value);
      if (Number.isNaN(numeric)) return 'Must be a number';
      if (field.type === 'integer' && !Number.isInteger(numeric)) {
        return 'Must be a whole number';
      }
      if (field.minimum !== undefined && numeric < field.minimum) {
        return `Must be at least ${field.minimum}`;
      }
      if (field.maximum !== undefined && numeric > field.maximum) {
        return `Must be at most ${field.maximum}`;
      }
      return null;
    }
    if (typeof value === 'string') {
      const text = value.trim();
      if (field.minLength !== undefined && text.length < field.minLength) {
        return `Needs at least ${field.minLength} characters`;
      }
      if (field.maxLength !== undefined && text.length > field.maxLength) {
        return `Longer than ${field.maxLength} characters`;
      }
      if (field.enum && !field.enum.map(String).includes(text)) {
        return 'Not one of the offered choices';
      }
    }
    return null;
  }

  private labelFor(name: string, field: QuestionField): string {
    return field.title || name.replace(/_/g, ' ');
  }

  private errorFor(path: string): string | null {
    if (!this.showErrors) return null;
    return this.errors[path] ?? null;
  }

  // ---------------------------------------------------------------- rendering

  render() {
    if (!this.schema || Object.keys(this.properties).length === 0) {
      return nothing;
    }
    return html`
      <div
        class="answer-form"
        part="form"
        role="group"
        aria-label="Answer form"
      >
        ${Object.entries(this.properties).map(([name, field]) =>
          this.renderField(name, field)
        )}
      </div>
    `;
  }

  private renderField(name: string, field: QuestionField): TemplateResult {
    if (field['x-autofill']) return this.renderAutofilled(name, field);
    if (field.type === 'array') return this.renderArray(name, field);
    if (field.type === 'object') return this.renderGroup(name, field);
    if (field.type === 'boolean') return this.renderBoolean(name, field);
    if (field.enum) return this.renderChoice(name, field);
    return this.renderScalar(name, field);
  }

  private renderLabel(name: string, field: QuestionField): TemplateResult {
    const required = this.requiredNames.includes(name);
    return html`
      <span class="field-label" id=${`label-${name}`}>
        ${this.labelFor(name, field)}
        ${
          required
            ? html`<span class="required-marker" aria-hidden="true">*</span>`
            : nothing
        }
      </span>
      ${
        field.description
          ? html`<p class="field-help">${field.description}</p>`
          : nothing
      }
    `;
  }

  private renderError(path: string): TemplateResult | typeof nothing {
    const message = this.errorFor(path);
    if (!message) return nothing;
    return html`<p class="field-error" role="alert">${message}</p>`;
  }

  /**
   * Who and when, filled by the platform.
   *
   * Never an input: the identity that decided is a fact the server knows, and
   * a name a person types into a waiver register is not attribution.
   */
  private renderAutofilled(name: string, field: QuestionField): TemplateResult {
    const kind = field['x-autofill'];
    const shown =
      kind === 'author'
        ? this.author || 'your account'
        : 'the moment you decide';
    return html`
      <div class="field" data-field=${name} data-autofill=${kind ?? ''}>
        ${this.renderLabel(name, field)}
        <div class="autofilled">
          <sl-icon name="shield-check"></sl-icon>
          <span>Filled in by Preloop: ${shown}</span>
        </div>
      </div>
    `;
  }

  private renderBoolean(name: string, field: QuestionField): TemplateResult {
    return html`
      <div class="field" data-field=${name}>
        <sl-switch
          class="field-switch"
          ?checked=${this.value[name] === true}
          ?disabled=${this.disabled}
          @sl-change=${(e: Event) =>
            this.setField(name, (e.target as HTMLInputElement).checked)}
        >
          ${this.labelFor(name, field)}
        </sl-switch>
        ${
          field.description
            ? html`<p class="field-help">${field.description}</p>`
            : nothing
        }
        ${this.renderError(name)}
      </div>
    `;
  }

  private renderChoice(name: string, field: QuestionField): TemplateResult {
    const choices = (field.enum ?? []).map(String);
    const current = this.value[name] == null ? '' : String(this.value[name]);
    const body =
      choices.length <= RADIO_LIMIT
        ? html`
            <sl-radio-group
              class="field-radios"
              label=${this.labelFor(name, field)}
              help-text=${field.description ?? ''}
              value=${current}
              @sl-change=${(e: Event) =>
                this.setField(name, (e.target as HTMLInputElement).value)}
            >
              ${choices.map(
                (choice) => html`
                  <sl-radio value=${choice} ?disabled=${this.disabled}
                    >${choice}</sl-radio
                  >
                `
              )}
            </sl-radio-group>
          `
        : html`
            <sl-select
              class="field-select"
              label=${this.labelFor(name, field)}
              help-text=${field.description ?? ''}
              value=${current}
              ?disabled=${this.disabled}
              @sl-change=${(e: Event) =>
                this.setField(name, (e.target as HTMLInputElement).value)}
            >
              ${choices.map(
                (choice) =>
                  html`<sl-option value=${choice}>${choice}</sl-option>`
              )}
            </sl-select>
          `;
    return html`
      <div class="field" data-field=${name}>
        ${body} ${this.renderError(name)}
      </div>
    `;
  }

  private renderScalar(name: string, field: QuestionField): TemplateResult {
    return html`
      <div class="field" data-field=${name}>
        ${this.renderScalarControl(
          name,
          field,
          this.value[name],
          (next) => this.setField(name, next),
          this.labelFor(name, field),
          field.description
        )}
        ${this.renderError(name)}
      </div>
    `;
  }

  /** One text, number or date control. Shared by top-level fields and rows. */
  private renderScalarControl(
    key: string,
    field: QuestionField,
    current: unknown,
    onInput: (next: unknown) => void,
    label: string,
    help?: string
  ): TemplateResult {
    const asText = current == null ? '' : String(current);
    if (field.type === 'string' && field.format === 'textarea') {
      return html`
        <sl-textarea
          class="field-input"
          data-key=${key}
          label=${label}
          help-text=${help ?? ''}
          rows="2"
          resize="auto"
          .value=${asText}
          ?disabled=${this.disabled}
          @sl-input=${(e: Event) =>
            onInput((e.target as HTMLTextAreaElement).value)}
        ></sl-textarea>
      `;
    }
    const type =
      field.type === 'number' || field.type === 'integer'
        ? 'number'
        : field.format === 'date'
          ? 'date'
          : field.format === 'date-time'
            ? 'datetime-local'
            : field.format === 'email'
              ? 'email'
              : 'text';
    return html`
      <sl-input
        class="field-input"
        data-key=${key}
        type=${type}
        label=${label}
        help-text=${help ?? ''}
        .value=${asText}
        ?disabled=${this.disabled}
        min=${field.minimum ?? nothing}
        max=${field.maximum ?? nothing}
        @sl-input=${(e: Event) => {
          const raw = (e.target as HTMLInputElement).value;
          if (field.type === 'number' || field.type === 'integer') {
            onInput(raw === '' ? null : Number(raw));
          } else {
            onInput(raw);
          }
        }}
      ></sl-input>
    `;
  }

  private renderGroup(name: string, field: QuestionField): TemplateResult {
    const group = (this.value[name] as Record<string, unknown>) ?? {};
    const setEntry = (key: string, next: unknown) => {
      this.setField(name, { ...group, [key]: next });
    };
    return html`
      <div class="field" data-field=${name}>
        <fieldset class="group">
          <legend class="field-label">${this.labelFor(name, field)}</legend>
          ${
            field.description
              ? html`<p class="field-help">${field.description}</p>`
              : nothing
          }
          ${Object.entries(field.properties ?? {}).map(([key, spec]) => {
            if (spec['x-autofill']) {
              return this.renderAutofilled(`${name}.${key}`, spec);
            }
            if (spec.type === 'boolean') {
              return html`
                <div class="field">
                  <sl-switch
                    ?checked=${group[key] === true}
                    ?disabled=${this.disabled}
                    @sl-change=${(e: Event) =>
                      setEntry(key, (e.target as HTMLInputElement).checked)}
                    >${this.labelFor(key, spec)}</sl-switch
                  >
                  ${this.renderError(`${name}.${key}`)}
                </div>
              `;
            }
            return html`
              <div class="field">
                ${this.renderScalarControl(
                  `${name}.${key}`,
                  spec,
                  group[key],
                  (next) => setEntry(key, next),
                  this.labelFor(key, spec),
                  spec.description
                )}
                ${this.renderError(`${name}.${key}`)}
              </div>
            `;
          })}
        </fieldset>
        ${this.renderError(name)}
      </div>
    `;
  }

  /**
   * The multi-select. Two shapes reach here: a list of ids
   * (`items: {enum: [...]}`) and a list of row objects
   * (`items: {type: object, properties: {id: {enum: [...]}, ...}}`), which is
   * the one that gives a reason per waived finding.
   */
  private renderArray(name: string, field: QuestionField): TemplateResult {
    const rowSpec = field.items;
    if (rowSpec?.type === 'object') {
      return this.renderRowTable(name, field, rowSpec);
    }
    if (rowSpec?.enum) {
      return this.renderIdChecklist(name, field, rowSpec.enum.map(String));
    }
    // A free list of scalars: one line each, comma-separated in, list out.
    const current = Array.isArray(this.value[name])
      ? (this.value[name] as unknown[]).join(', ')
      : '';
    return html`
      <div class="field" data-field=${name}>
        <sl-input
          class="field-input"
          label=${this.labelFor(name, field)}
          help-text=${field.description ?? 'Separate entries with a comma'}
          .value=${current}
          ?disabled=${this.disabled}
          @sl-input=${(e: Event) => {
            const raw = (e.target as HTMLInputElement).value;
            const parts = raw
              .split(',')
              .map((part) => part.trim())
              .filter(Boolean);
            this.setField(name, parts);
          }}
        ></sl-input>
        ${this.renderError(name)}
      </div>
    `;
  }

  /** The rows the checkboxes are for: real items where we have them. */
  private rowsForChoices(choices: string[]): QuestionItem[] {
    const byId = new Map((this.items ?? []).map((item) => [item.id, item]));
    if (choices.length > 0) {
      return choices.map((id) => byId.get(id) ?? { id, title: id });
    }
    return this.items ?? [];
  }

  private renderItemCell(item: QuestionItem): TemplateResult {
    return html`
      <div class="item-title">
        ${
          item.href
            ? html`<a
                href=${item.href}
                target="_blank"
                rel="noopener noreferrer"
                >${item.title || item.id}</a
              >`
            : html`${item.title || item.id}`
        }
      </div>
      ${
        item.description
          ? html`<div class="item-detail">${item.description}</div>`
          : nothing
      }
      ${
        item.severity || (item.badges && item.badges.length > 0)
          ? html`
              <div class="item-chips">
                ${
                  item.severity
                    ? html`<sl-badge
                        pill
                        class="chip severity-chip"
                        variant=${this.severityVariant(item.severity)}
                        >${item.severity}</sl-badge
                      >`
                    : nothing
                }
                ${(item.badges ?? []).map(
                  (badge) =>
                    html`<sl-badge pill class="chip" variant="neutral"
                      >${badge}</sl-badge
                    >`
                )}
              </div>
            `
          : nothing
      }
    `;
  }

  private severityVariant(
    severity: string
  ): 'danger' | 'warning' | 'neutral' | 'primary' {
    const normalized = severity.toLowerCase();
    if (normalized === 'critical' || normalized === 'high') return 'danger';
    if (normalized === 'medium') return 'warning';
    return 'neutral';
  }

  private renderIdChecklist(
    name: string,
    field: QuestionField,
    choices: string[]
  ): TemplateResult {
    const selected = new Set(this.idsOf(name));
    const rows = this.rowsForChoices(choices);
    const toggle = (id: string, checked: boolean) => {
      const next = choices.filter((choice) =>
        choice === id ? checked : selected.has(choice)
      );
      this.setField(name, next);
    };
    return html`
      <div class="field" data-field=${name}>
        ${this.renderLabel(name, field)}
        <table class="item-table">
          <thead>
            <tr>
              <th scope="col">Pick</th>
              <th scope="col">Item</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(
              (item) => html`
                <tr data-item-id=${item.id}>
                  <td>
                    <sl-checkbox
                      class="item-checkbox"
                      value=${item.id}
                      ?checked=${selected.has(item.id)}
                      ?disabled=${this.disabled}
                      @sl-change=${(e: Event) =>
                        toggle(item.id, (e.target as HTMLInputElement).checked)}
                    ></sl-checkbox>
                  </td>
                  <td>${this.renderItemCell(item)}</td>
                </tr>
              `
            )}
          </tbody>
        </table>
        ${this.renderError(name)}
      </div>
    `;
  }

  private renderRowTable(
    name: string,
    field: QuestionField,
    rowSpec: QuestionField
  ): TemplateResult {
    const properties = rowSpec.properties ?? {};
    const choices = (properties.id?.enum ?? []).map(String);
    const rows = this.rowsForChoices(choices);
    const extraKeys = Object.keys(properties).filter(
      (key) => key !== 'id' && !properties[key]['x-autofill']
    );
    const columnLabel =
      extraKeys.length === 1
        ? this.labelFor(extraKeys[0], properties[extraKeys[0]])
        : 'Details';

    return html`
      <div class="field" data-field=${name}>
        ${this.renderLabel(name, field)}
        <table class="item-table">
          <thead>
            <tr>
              <th scope="col">Pick</th>
              <th scope="col">Item</th>
              ${
                extraKeys.length > 0
                  ? html`<th scope="col">${columnLabel}</th>`
                  : nothing
              }
            </tr>
          </thead>
          <tbody>
            ${rows.map((item) =>
              this.renderPickRow(name, item, properties, extraKeys, columnLabel)
            )}
          </tbody>
        </table>
        ${this.renderError(name)}
      </div>
    `;
  }

  /** One finding: the checkbox, what it is, and its fields once picked. */
  private renderPickRow(
    name: string,
    item: QuestionItem,
    properties: Record<string, QuestionField>,
    extraKeys: string[],
    columnLabel: string
  ): TemplateResult {
    const selected = this.rowsOf(name);
    const index = selected.findIndex((row) => String(row.id) === item.id);
    const row = index >= 0 ? selected[index] : null;

    const toggle = (checked: boolean) => {
      if (checked) {
        if (index >= 0) return;
        this.setField(name, [...selected, { id: item.id }]);
      } else {
        this.setField(
          name,
          selected.filter((entry) => String(entry.id) !== item.id)
        );
      }
    };

    const fieldsCell =
      extraKeys.length === 0
        ? nothing
        : html`<td>
            ${this.renderRowFieldsCell(
              name,
              item,
              index,
              properties,
              extraKeys,
              columnLabel
            )}
          </td>`;

    return html`
      <tr data-item-id=${item.id}>
        <td>
          <sl-checkbox
            class="item-checkbox"
            value=${item.id}
            ?checked=${row !== null}
            ?disabled=${this.disabled}
            @sl-change=${(e: Event) =>
              toggle((e.target as HTMLInputElement).checked)}
          ></sl-checkbox>
        </td>
        <td>${this.renderItemCell(item)}</td>
        ${fieldsCell}
      </tr>
    `;
  }

  /** The per-row fields, or a hint that the row has to be picked first. */
  private renderRowFieldsCell(
    name: string,
    item: QuestionItem,
    index: number,
    properties: Record<string, QuestionField>,
    extraKeys: string[],
    columnLabel: string
  ): TemplateResult {
    const row = index >= 0 ? this.rowsOf(name)[index] : null;
    if (row === null) {
      const hint = `Pick this row to fill in ${columnLabel.toLowerCase()}`;
      return html`<span class="row-disabled-hint">${hint}</span>`;
    }
    return html`
      <div class="row-fields">
        ${extraKeys.map((key) =>
          this.renderRowField(name, item.id, index, key, properties[key], row)
        )}
      </div>
    `;
  }

  /** One per-row field (the reason a finding is waived). */
  private renderRowField(
    name: string,
    itemId: string,
    index: number,
    key: string,
    spec: QuestionField,
    row: Record<string, unknown>
  ): TemplateResult {
    const path = `${name}[${index}].${key}`;
    const label = this.labelFor(key, spec);
    const setRowField = (next: unknown) => {
      this.setField(
        name,
        this.rowsOf(name).map((entry) =>
          String(entry.id) === itemId ? { ...entry, [key]: next } : entry
        )
      );
    };
    const control =
      spec.type === 'boolean'
        ? this.renderRowSwitch(label, row[key] === true, setRowField)
        : this.renderScalarControl(
            path,
            spec,
            row[key],
            setRowField,
            label,
            spec.description
          );
    return html`<div>${control} ${this.renderError(path)}</div>`;
  }

  /** A yes/no per-row field, as a switch rather than a checkbox. */
  private renderRowSwitch(
    label: string,
    checked: boolean,
    setRowField: (next: unknown) => void
  ): TemplateResult {
    return html`<sl-switch
      ?checked=${checked}
      ?disabled=${this.disabled}
      @sl-change=${(e: Event) =>
        setRowField((e.target as HTMLInputElement).checked)}
      >${label}</sl-switch
    >`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'answer-form': AnswerForm;
  }
}
