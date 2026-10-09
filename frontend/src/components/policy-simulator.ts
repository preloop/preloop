import { LitElement, html } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { evaluatePolicy, type PolicyEvaluationResult } from '../api';
import { ruleActionLabel } from '../utils/rule-actions';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';

/** Draft testing shared by the tool rule dialog and YAML editor. */
@customElement('policy-simulator')
export class PolicySimulator extends LitElement {
  @property() toolName = '';
  @property() server = 'builtin';
  @property() draftYaml: string | undefined;
  @property({ attribute: false }) draftRule:
    Record<string, unknown> | undefined;
  @property({ attribute: false }) toolSchema: Record<string, any> | null = null;
  @state() private _args = '{}';
  @state() private _modelText = '';
  @state() private _busy = false;
  @state() private _result: PolicyEvaluationResult | null = null;
  @state() private _error = '';
  private _policySnapshot = '';

  createRenderRoot() {
    return this;
  }

  protected willUpdate(changed: Map<string, unknown>) {
    if (changed.has('toolSchema') && this.toolSchema) {
      const defaults: Record<string, unknown> = {
        string: '',
        number: 0,
        integer: 0,
        boolean: false,
        array: [],
        object: {},
      };
      this._args = JSON.stringify(
        Object.fromEntries(
          Object.entries(this.toolSchema.properties || {}).map(
            ([key, field]: [string, any]) => [
              key,
              field.example ??
                field.default ??
                field.enum?.[0] ??
                defaults[field.type] ??
                null,
            ]
          )
        ),
        null,
        2
      );
    }
    if (
      ['draftRule', 'draftYaml', 'toolName', 'server'].some((key) =>
        changed.has(key)
      )
    ) {
      const snapshot = JSON.stringify([
        this.draftRule,
        this.draftYaml,
        this.toolName,
        this.server,
      ]);
      if (snapshot !== this._policySnapshot) this._result = null;
      this._policySnapshot = snapshot;
    }
  }

  private async _simulate() {
    if (this._busy) return;
    this._busy = true;
    this._error = '';
    this._result = null;
    try {
      const args = JSON.parse(this._args);
      if (!args || typeof args !== 'object' || Array.isArray(args))
        throw new Error('Sample arguments must be a JSON object.');
      this._result = await evaluatePolicy({
        name: this.toolName,
        server: this.server,
        args,
        ...(this.draftYaml !== undefined
          ? { draft_yaml: this.draftYaml }
          : { draft_rule: this.draftRule }),
        ...(this._modelText ? { model_text: this._modelText } : {}),
      });
    } catch (err) {
      this._error =
        err instanceof Error ? err.message : 'Failed to simulate policy';
    } finally {
      this._busy = false;
    }
  }

  render() {
    return html`<section
      aria-label=${this.draftYaml !== undefined ? 'Simulate draft policy' : 'Test this rule'}
    >
      <h4>
        ${this.draftYaml !== undefined ? 'Simulate draft policy' : 'Test this rule'}
      </h4>
      ${
        this.draftYaml !== undefined
          ? html` <sl-input
                label="Tool name"
                .value=${this.toolName}
                @sl-input=${(e: any) => {
                  this.toolName = e.target.value;
                }}
              ></sl-input>
              <sl-input
                label="Tool server or source"
                .value=${this.server}
                @sl-input=${(e: any) => {
                  this.server = e.target.value;
                }}
              ></sl-input>
              <sl-textarea
                label="Model request text (optional)"
                .value=${this._modelText}
                @sl-input=${(e: any) => {
                  this._modelText = e.target.value;
                }}
              ></sl-textarea>`
          : ''
      }
      <sl-textarea
        label="Sample arguments (JSON)"
        .value=${this._args}
        @sl-input=${(e: any) => {
          this._args = e.target.value;
        }}
      ></sl-textarea>
      <p>
        For path rules, also try /etc/../etc and //etc. Conditions use the
        arguments as supplied; paths are not normalized.
      </p>
      <sl-button
        ?loading=${this._busy}
        ?disabled=${!this.toolName || this._busy}
        @click=${this._simulate}
        >Simulate</sl-button
      >
      <div role="status" aria-live="polite">
        ${this._error || (this._result ? html`→ ${ruleActionLabel(this._result.decision)}${this._result.matched_rule ? ` (rule ${this._result.matched_rule})` : ''}` : '')}
      </div>
      ${
        this._result
          ? html`<ol>
                ${this._result.checked_rules.map((rule) => html`<li>${rule.id}: ${rule.error ? `Error: ${rule.error}` : rule.matched ? 'Matched' : 'No match'}</li>`)}
              </ol>
              ${this._result.also_matched_rule_ids.length ? html`<p>Also matched: ${this._result.also_matched_rule_ids.join(', ')}</p>` : ''}`
          : ''
      }
    </section>`;
  }
}
