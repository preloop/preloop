import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  getFeatures,
  getUserProfile,
  getRoles,
  getTeams,
  hasPermission,
} from '../api';
import {
  runnerPolicyRequest,
  type RunnerPolicy,
  type RunnerPolicyEntry,
  type RunnerRequirement,
} from '../api/runner-policy';

/** Account-scoped editor for the optional EE policy provider. */
@customElement('runner-policy-settings')
export class RunnerPolicySettings extends LitElement {
  @state() private policy: RunnerPolicy | null = null;
  @state() private canEdit = false;
  @state() private enabled = false;
  @state() private error = '';
  @state() private notice = '';
  @state() private saving = false;
  @state() private choices: {
    scope: 'roles' | 'teams';
    id: string;
    name: string;
  }[] = [];
  private generation = 0;

  static styles = css`
    :host {
      display: block;
    }
    h2 {
      margin: 0 0 0.5rem;
      font-size: var(--console-text-card-title, 0.9375rem);
      font-weight: 600;
    }
    section {
      padding: 1.25rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 0.5rem;
    }
    fieldset {
      margin: 1rem 0;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 0.4rem;
    }
    .fields {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      align-items: end;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      margin: 0.5rem 0;
    }
    .check {
      flex-direction: row;
      align-items: center;
    }
    input,
    select,
    textarea,
    button {
      font: inherit;
      padding: 0.45rem;
    }
    input[type='number'] {
      max-width: 7rem;
    }
    textarea {
      width: min(30rem, 85%);
    }
    p {
      line-height: 1.5;
    }
    button {
      cursor: pointer;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }
  disconnectedCallback(): void {
    ++this.generation;
    this.policy = null;
    this.enabled = false;
    this.canEdit = false;
    this.saving = false;
    this.choices = [];
    this.error = '';
    this.notice = '';
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    try {
      const features = await getFeatures();
      if (
        generation !== this.generation ||
        !features.features.runner_requirement_policy
      )
        return;
      const user = await getUserProfile();
      if (
        generation !== this.generation ||
        !hasPermission(user.permissions, 'view_policies')
      )
        return;
      this.enabled = true;
      this.canEdit = hasPermission(user.permissions, 'manage_policies');
      const policy = await runnerPolicyRequest<RunnerPolicy>('runner-policy');
      if (generation !== this.generation) return;
      this.policy = policy;
      if (this.canEdit) {
        // Listing scopes is optional: an unavailable directory must not hide the policy.
        const results = await Promise.allSettled([
          hasPermission(user.permissions, 'view_users')
            ? getRoles()
            : Promise.resolve({ roles: [], total: 0 }),
          hasPermission(user.permissions, 'view_teams')
            ? this.teamChoices()
            : Promise.resolve([]),
        ]);
        if (generation !== this.generation) return;
        this.choices = [
          ...(results[0].status === 'fulfilled'
            ? results[0].value.roles.map((r) => ({
                scope: 'roles' as const,
                id: r.id,
                name: r.name,
              }))
            : []),
          ...(results[1].status === 'fulfilled' ? results[1].value : []),
        ];
      }
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not load runner policy.';
    }
  }

  private async teamChoices(): Promise<
    { scope: 'teams'; id: string; name: string }[]
  > {
    const choices: { scope: 'teams'; id: string; name: string }[] = [];
    let skip = 0;
    while (true) {
      const page = await getTeams(skip, 100);
      choices.push(
        ...page.teams.map((t) => ({
          scope: 'teams' as const,
          id: t.id,
          name: t.name,
        }))
      );
      skip += page.teams.length;
      if (skip >= page.total || !page.teams.length) return choices;
    }
  }

  private change(
    entry: RunnerPolicyEntry,
    field: keyof RunnerPolicyEntry,
    value: unknown
  ): void {
    if (!this.canEdit || this.saving) return;
    Object.assign(entry, { [field]: value });
    if (entry.requirement !== 'required') entry.mandated_capabilities = [];
    this.notice = '';
    this.requestUpdate();
  }

  private addScope(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const choice = this.choices.find(
      (c) => `${c.scope}:${c.id}` === select.value
    );
    if (choice && this.policy && this.canEdit && !this.saving) {
      this.policy[choice.scope][choice.id] = {
        requirement: 'optional',
        mandated_capabilities: [],
        grace_days: 7,
        enforcement: 'none',
      };
      this.requestUpdate();
    }
    select.value = '';
  }

  private async save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!this.canEdit || this.saving || !this.policy) return;
    const generation = this.generation;
    this.saving = true;
    this.error = '';
    this.notice = '';
    try {
      const saved = await runnerPolicyRequest<RunnerPolicy>('runner-policy', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.policy),
      });
      if (generation !== this.generation) return;
      this.policy = saved;
      this.notice = 'Runner policy saved.';
      window.dispatchEvent(new Event('runner-policy-changed'));
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not save runner policy.';
    } finally {
      if (generation === this.generation) this.saving = false;
    }
  }

  private entry(
    entry: RunnerPolicyEntry,
    title: string,
    scope?: 'roles' | 'teams',
    id?: string
  ) {
    return html`<fieldset ?disabled=${!this.canEdit || this.saving}>
      <legend>${title}</legend>
      <div class="fields">
        <label
          >Requirement<select
            .value=${entry.requirement}
            @change=${(e: Event) => this.change(entry, 'requirement', (e.target as HTMLSelectElement).value as RunnerRequirement)}
          >
            <option value="optional">Optional</option>
            <option value="required">Required</option>
            <option value="forbidden">Forbidden</option>
          </select></label
        >
        <label
          >Grace period (days)<input
            type="number"
            min="0"
            max="90"
            required
            .value=${String(entry.grace_days)}
            @input=${(e: Event) => this.change(entry, 'grace_days', (e.target as HTMLInputElement).valueAsNumber)}
        /></label>
        <label
          >Enforcement<select
            .value=${entry.enforcement}
            @change=${(e: Event) => this.change(entry, 'enforcement', (e.target as HTMLSelectElement).value)}
          >
            <option value="none">Report only</option>
            <option value="warn">Warn member and digest</option>
          </select></label
        >
      </div>
      ${
        entry.requirement === 'required'
          ? html`<p>Required capabilities</p>
              ${(['flows', 'inventory', 'sessions'] as const).map((cap) => html`<label class="check"><input type="checkbox" .checked=${entry.mandated_capabilities.includes(cap)} @change=${(e: Event) => this.change(entry, 'mandated_capabilities', (e.target as HTMLInputElement).checked ? [...entry.mandated_capabilities, cap] : entry.mandated_capabilities.filter((c) => c !== cap))} />${cap === 'flows' ? 'Flow execution' : cap === 'inventory' ? 'Harness inventory' : 'Remote sessions'}</label>`)}`
          : nothing
      }
      <label
        >Member notice<textarea
          maxlength="500"
          .value=${entry.message ?? ''}
          @input=${(e: Event) => this.change(entry, 'message', (e.target as HTMLTextAreaElement).value)}
        ></textarea>
      </label>
      ${
        scope && id
          ? html`<button
              type="button"
              @click=${() => {
                if (this.policy && this.canEdit && !this.saving) {
                  delete this.policy[scope][id];
                  this.requestUpdate();
                }
              }}
            >
              Remove override
            </button>`
          : nothing
      }
    </fieldset>`;
  }

  render() {
    if (!this.enabled) return nothing;
    return html`<section aria-label="Runner requirement policy">
      <h2>Personal runner policy</h2>
      <p>
        Required installs a runner at interactive login with a notice. Forbidden
        suppresses installation. Members can still use unenrolled machines; this
        policy does not block flow routing.
        <a href="https://docs.preloop.ai/guide/runners/policy/">Runner setup</a>
      </p>
      <p>
        Team overrides take precedence over role overrides, then the account
        default. Within one scope, required takes precedence over forbidden,
        then optional.
      </p>
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}${this.notice ? html`<p role="status">${this.notice}</p>` : nothing}
      ${
        this.policy
          ? html`<form @submit=${this.save}>
              ${!this.canEdit ? html`<p>Read only. Managing policies permission is required to edit.</p>` : nothing}
              ${this.entry(this.policy.account, 'Account default')}
              ${(['roles', 'teams'] as const).map((scope) => Object.entries(this.policy![scope]).map(([id, entry]) => this.entry(entry, `${scope === 'roles' ? 'Role' : 'Team'}: ${this.choices.find((c) => c.scope === scope && c.id === id)?.name ?? id}`, scope, id)))}
              ${
                this.canEdit
                  ? html`<label
                      >Add role or team override<select
                        aria-label="Add role or team override"
                        ?disabled=${this.saving}
                        @change=${this.addScope}
                      >
                        <option value="">Choose a role or team</option>
                        ${this.choices.filter((c) => !this.policy![c.scope][c.id]).map((c) => html`<option value=${`${c.scope}:${c.id}`}>${c.scope === 'roles' ? 'Role' : 'Team'}: ${c.name}</option>`)}
                      </select></label
                    >`
                  : nothing
              }
              <label
                >Runner becomes stale after (days)<input
                  type="number"
                  min="1"
                  max="30"
                  required
                  ?disabled=${!this.canEdit || this.saving}
                  .value=${String(this.policy.stale_after_days)}
                  @input=${(e: Event) => {
                    if (this.canEdit && !this.saving)
                      this.policy = {
                        ...this.policy!,
                        stale_after_days: (e.target as HTMLInputElement)
                          .valueAsNumber,
                      };
                  }}
              /></label>
              ${this.canEdit ? html`<button type="submit" ?disabled=${this.saving}>${this.saving ? 'Saving…' : 'Save runner policy'}</button>` : nothing}
            </form>`
          : nothing
      }
    </section>`;
  }
}
