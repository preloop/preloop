import { html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { AuthedElement } from '../api';

interface ReadinessPolicy {
  version: string;
  required_build_keys: string[];
  minimum_approvals: number;
  changes_requests_block: boolean;
  unresolved_tasks_block: boolean;
}

@customElement('readiness-policy-settings')
export class ReadinessPolicySettings extends AuthedElement {
  @property({ attribute: 'project-id' }) projectId = '';
  @state() private keys = '';
  @state() private approvals = '';
  @state() private blockChanges = true;
  @state() private blockTasks = true;
  @state() private version = '';
  @state() private error = '';
  @state() private saving = false;

  protected updated(changed: Map<PropertyKey, unknown>) {
    if (changed.has('projectId')) void this.loadPolicy();
  }

  private async loadPolicy() {
    const project = this.projectId;
    this.version = '';
    this.keys = '';
    this.approvals = '';
    this.error = '';
    if (!project) return;
    try {
      const policy = (await this.fetchData(
        `/api/v1/projects/${encodeURIComponent(project)}/readiness-policy`
      )) as ReadinessPolicy | null;
      if (project !== this.projectId || !policy) return;
      this.version = policy.version;
      this.keys = JSON.stringify(policy.required_build_keys);
      this.approvals = String(policy.minimum_approvals);
      this.blockChanges = policy.changes_requests_block;
      this.blockTasks = policy.unresolved_tasks_block;
    } catch {
      if (project === this.projectId)
        this.error = 'Could not load readiness policy.';
    }
  }

  private async savePolicy(event: SubmitEvent) {
    event.preventDefault();
    this.error = '';
    let keys: unknown;
    try {
      keys = JSON.parse(this.keys);
    } catch {
      this.error =
        'Enter required build keys as a JSON array, including [] for an explicit empty set.';
      return;
    }
    if (
      !Array.isArray(keys) ||
      keys.some((key) => typeof key !== 'string' || !key.trim()) ||
      new Set(keys).size !== keys.length ||
      !/^\d+$/.test(this.approvals)
    ) {
      this.error =
        'Enter unique nonempty build keys and an explicit nonnegative approval count.';
      return;
    }
    const project = this.projectId;
    this.saving = true;
    try {
      const policy = await this.fetchData(
        `/api/v1/projects/${encodeURIComponent(project)}/readiness-policy`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            required_build_keys: keys,
            minimum_approvals: Number(this.approvals),
            changes_requests_block: this.blockChanges,
            unresolved_tasks_block: this.blockTasks,
          }),
        }
      );
      if (!policy) throw new Error('Unavailable');
      if (project !== this.projectId) return;
      this.version = policy.version;
      this.dispatchEvent(
        new CustomEvent('readiness-policy-saved', {
          bubbles: true,
          composed: true,
        })
      );
    } catch {
      this.error =
        'Could not save readiness policy. Project edit permission is required.';
    } finally {
      this.saving = false;
    }
  }

  private async disablePolicy() {
    const project = this.projectId;
    this.saving = true;
    this.error = '';
    try {
      const result = await this.fetchData(
        `/api/v1/projects/${encodeURIComponent(project)}/readiness-policy`,
        { method: 'DELETE' }
      );
      if (!result) throw new Error('Unavailable');
      if (project === this.projectId) {
        this.version = '';
        this.keys = '';
        this.approvals = '';
        this.dispatchEvent(
          new CustomEvent('readiness-policy-saved', {
            bubbles: true,
            composed: true,
          })
        );
      }
    } catch {
      if (project === this.projectId)
        this.error = 'Could not disable readiness policy.';
    } finally {
      this.saving = false;
    }
  }

  render() {
    if (!this.projectId) return nothing;
    return html`<details>
      <summary>Configured readiness policy</summary>
      <p>
        Observed ready under configured policy requires an open, non-draft PR
        and a conflict-free merge of the observed commits. Forge restrictions
        remain unknown. Each save starts a new policy series.
      </p>
      <p>
        ${this.version ? `Policy version: ${this.version}` : 'Unconfigured: ticket readiness is unknown.'}
      </p>
      <form @submit=${this.savePolicy}>
        <label
          >Required build keys (JSON array)<input
            required
            aria-label="Required build keys"
            .value=${this.keys}
            placeholder='["build"]'
            @input=${(e: Event) => (this.keys = (e.target as HTMLInputElement).value)}
        /></label>
        <label
          >Minimum approvals<input
            required
            type="number"
            min="0"
            step="1"
            aria-label="Minimum approvals"
            .value=${this.approvals}
            @input=${(e: Event) => (this.approvals = (e.target as HTMLInputElement).value)}
        /></label>
        <label
          ><input
            type="checkbox"
            .checked=${this.blockChanges}
            @change=${(e: Event) => (this.blockChanges = (e.target as HTMLInputElement).checked)}
          />Changes requests block readiness</label
        >
        <label
          ><input
            type="checkbox"
            .checked=${this.blockTasks}
            @change=${(e: Event) => (this.blockTasks = (e.target as HTMLInputElement).checked)}
          />Unresolved tasks block readiness</label
        >
        <button type="submit" ?disabled=${this.saving}>
          ${this.saving ? 'Saving…' : 'Save new policy version'}
        </button>
      </form>
      ${this.version ? html`<button type="button" ?disabled=${this.saving} @click=${this.disablePolicy}>Disable configured policy</button>` : nothing}
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
    </details>`;
  }
}
