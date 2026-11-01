import { LitElement, html, css, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import { getTeams, getUserProfile, getUsers } from '../../../api';
import {
  NO_CAPABILITIES,
  isCapabilityOff,
  type CapabilitySet,
} from '../../../capabilities';
import {
  answerRunnerShare,
  createRunnerShare,
  getRunnerSharing,
  revokeRunnerShare,
  setAccountOwnerConsent,
  setRunnerConsentSetting,
  type OwnerConsent,
  type OwnerConsentPolicy,
  type RunnerConsentSetting,
  type RunnerShare,
  type RunnerSharing,
  type ShareScope,
} from '../../../runner-sharing-api';

/** One runner as the runners page passes it in. */
export interface SharingRunner {
  id: string;
  name: string;
  owner_user_id?: string | null;
}

interface Grantee {
  key: string;
  type: 'team' | 'user';
  id: string;
  label: string;
}

const SOURCE_LABEL: Record<OwnerConsentPolicy['source'], string> = {
  runner: 'set for this runner',
  account: 'account setting',
  default: 'default',
};

/** Comma or space separated ids; empty means "all". */
function parseIds(text: string): string[] | null {
  const ids = text
    .split(/[\s,]+/)
    .map((id) => id.trim())
    .filter(Boolean);
  return ids.length ? ids : null;
}

/**
 * Runner sharing (EE #186): who else may use a personal runner, for flows or
 * sessions, limited to some harnesses and directories, and the owner consent
 * policy that applies. Context: `runners`, an array of {@link SharingRunner}.
 */
@customElement('runner-sharing-panel')
export class RunnerSharingPanel extends LitElement {
  static styles = css`
    :host {
      display: block;
      margin-top: var(--sl-spacing-large);
      font-size: var(--console-text-body, 0.875rem);
    }
    h3 {
      font-size: var(--console-text-card-title, 0.9375rem);
      font-weight: 600;
      margin: 0 0 var(--sl-spacing-x-small);
    }
    .meta {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      font-size: var(--console-text-meta, 0.8125rem);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-x-small) 0;
      border-bottom: 1px solid
        var(--console-hairline, var(--sl-color-neutral-200));
    }
    .row .who {
      font-weight: 500;
      min-width: 10rem;
    }
    .form {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: var(--sl-spacing-small);
      margin-top: var(--sl-spacing-medium);
    }
    .policy {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sl-spacing-small);
      margin: var(--sl-spacing-small) 0;
    }
    sl-badge::part(base) {
      font-size: 0.75rem;
      font-weight: 500;
      padding: 2px 8px;
      border: none;
      color: var(--sl-color-neutral-800);
      background: color-mix(
        in srgb,
        var(--sl-color-neutral-500) 16%,
        transparent
      );
    }
    sl-badge[variant='success']::part(base) {
      color: var(--sl-color-success-800);
      background: color-mix(
        in srgb,
        var(--sl-color-success-500) 16%,
        transparent
      );
    }
    sl-badge[variant='warning']::part(base) {
      color: var(--sl-color-warning-800);
      background: color-mix(
        in srgb,
        var(--sl-color-warning-500) 16%,
        transparent
      );
    }
    sl-badge[variant='danger']::part(base) {
      color: var(--sl-color-danger-800);
      background: color-mix(
        in srgb,
        var(--sl-color-danger-500) 16%,
        transparent
      );
    }
    .error {
      color: var(--sl-color-danger-700);
      margin-top: var(--sl-spacing-x-small);
    }
  `;

  @property({ attribute: false }) context: Record<string, unknown> = {};
  @property({ attribute: false }) capabilities: CapabilitySet = NO_CAPABILITIES;

  @state() private runnerId = '';
  @state() private sharing: RunnerSharing | null = null;
  @state() private grantees: Grantee[] = [];
  @state() private me = '';
  @state() private error = '';
  @state() private busy = false;
  @state() private granteeKey = '';
  @state() private scopes: ShareScope[] = ['sessions'];
  @state() private harnesses = '';
  @state() private directories = '';
  /** The server does not serve runner sharing: render nothing. */
  @state() private off = false;
  /** The caller cannot list users or teams, so cannot pick a grantee. */
  @state() private peopleUnavailable = false;
  /** Bumped on every load; a response for an older load is dropped. */
  private generation = 0;

  private runners(): SharingRunner[] {
    const value = this.context.runners;
    return Array.isArray(value) ? (value as SharingRunner[]) : [];
  }

  connectedCallback(): void {
    super.connectedCallback();
    void this.loadPeople();
  }

  protected updated(changed: PropertyValues): void {
    if (changed.has('runnerId') && this.runnerId) void this.load();
  }

  private async loadPeople(): Promise<void> {
    const [profile, teams, users] = await Promise.all([
      getUserProfile().catch(() => null),
      getTeams().catch(() => null),
      getUsers().catch(() => null),
    ]);
    this.me = profile?.id ?? '';
    this.peopleUnavailable = teams === null && users === null;
    this.grantees = [
      ...(teams?.teams ?? []).map((t) => ({
        key: `team:${t.id}`,
        type: 'team' as const,
        id: t.id,
        label: `Team ${t.name}`,
      })),
      ...(users?.users ?? []).map((u) => ({
        key: `user:${u.id}`,
        type: 'user' as const,
        id: u.id,
        label: u.username || u.email || u.id,
      })),
    ];
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    const runnerId = this.runnerId;
    this.error = '';
    this.sharing = null;
    try {
      const sharing = await getRunnerSharing(runnerId);
      if (generation !== this.generation || runnerId !== this.runnerId) return;
      this.sharing = sharing;
    } catch (error) {
      if (generation !== this.generation) return;
      if (isCapabilityOff(error)) {
        this.off = true;
        return;
      }
      this.error = (error as Error).message;
    }
  }

  private async act(work: () => Promise<unknown>): Promise<void> {
    this.busy = true;
    this.error = '';
    try {
      await work();
      await this.load();
    } catch (error) {
      this.error = (error as Error).message;
    } finally {
      this.busy = false;
    }
  }

  private granteeLabel(share: RunnerShare): string {
    const found = this.grantees.find(
      (g) => g.type === share.grantee_type && g.id === share.grantee_id
    );
    if (found) return found.label;
    return `${share.grantee_type === 'team' ? 'Team' : 'User'} ${share.grantee_id.slice(0, 8)}`;
  }

  private stateChip(share: RunnerShare) {
    if (share.revoked_at) {
      return html`<sl-badge variant="neutral" pill>Revoked</sl-badge>`;
    }
    if (share.consent_state === 'pending_owner_consent') {
      return html`<sl-badge variant="warning" pill
        >Waiting for owner</sl-badge
      >`;
    }
    if (share.consent_state === 'declined') {
      return html`<sl-badge variant="danger" pill>Declined</sl-badge>`;
    }
    return html`<sl-badge variant="success" pill
      >${share.consent_source === 'admin_override' ? 'Active (admin override)' : 'Active'}</sl-badge
    >`;
  }

  private renderPolicy(sharing: RunnerSharing) {
    const policy = sharing.policy;
    const label =
      policy.owner_consent === 'required'
        ? 'Owner consent required'
        : 'Owner consent not required';
    return html`<div class="policy" data-testid="effective-policy">
        <span>${label}</span>
        <span class="meta">(${SOURCE_LABEL[policy.source]})</span>
        ${
          sharing.can_set_policy
            ? html`<sl-select
                  size="small"
                  label="This runner"
                  data-testid="runner-policy"
                  .value=${policy.runner_setting}
                  ?disabled=${this.busy}
                  @sl-change=${(e: Event) =>
                    this.act(() =>
                      setRunnerConsentSetting(
                        this.runnerId,
                        (e.target as HTMLSelectElement)
                          .value as RunnerConsentSetting
                      )
                    )}
                >
                  <sl-option value="inherit">Follow account</sl-option>
                  <sl-option value="required">Require owner consent</sl-option>
                  <sl-option value="not_required">Admins may share</sl-option>
                </sl-select>
                <sl-select
                  size="small"
                  label="Account"
                  data-testid="account-policy"
                  .value=${policy.account_setting}
                  ?disabled=${this.busy}
                  @sl-change=${(e: Event) =>
                    this.act(() =>
                      setAccountOwnerConsent(
                        (e.target as HTMLSelectElement).value as OwnerConsent
                      )
                    )}
                >
                  <sl-option value="required">Require owner consent</sl-option>
                  <sl-option value="not_required">Admins may share</sl-option>
                </sl-select>`
            : nothing
        }
      </div>
      ${
        policy.owner_consent === 'not_required'
          ? html`<div class="meta">
              Admin shares are active without the owner, and session starts skip
              the owner's approval.
            </div>`
          : nothing
      }`;
  }

  private renderShare(sharing: RunnerSharing, share: RunnerShare) {
    const isOwner = Boolean(this.me) && sharing.owner_user_id === this.me;
    const live = !share.revoked_at;
    return html`<div class="row" data-testid="share-row">
      <span class="who">${this.granteeLabel(share)}</span>
      <span>${share.scopes.join(', ')}</span>
      <span class="meta"
        >Harnesses: ${share.harnesses?.join(', ') ?? 'all'} · Directories:
        ${share.directory_ids?.join(', ') ?? 'all'}</span
      >
      ${this.stateChip(share)}
      ${
        live && isOwner && share.consent_state === 'pending_owner_consent'
          ? html`<sl-button
                size="small"
                variant="primary"
                data-testid="accept-share"
                ?disabled=${this.busy}
                @click=${() =>
                  this.act(() =>
                    answerRunnerShare(share.runner_id, share.id, true)
                  )}
                >Accept</sl-button
              >
              <sl-button
                size="small"
                data-testid="decline-share"
                ?disabled=${this.busy}
                @click=${() =>
                  this.act(() =>
                    answerRunnerShare(share.runner_id, share.id, false)
                  )}
                >Decline</sl-button
              >`
          : nothing
      }
      ${
        live && sharing.can_share
          ? html`<sl-button
              size="small"
              data-testid="revoke-share"
              ?disabled=${this.busy}
              @click=${() =>
                this.act(() => revokeRunnerShare(share.runner_id, share.id))}
              >Revoke</sl-button
            >`
          : nothing
      }
    </div>`;
  }

  private toggleScope(scope: ShareScope, on: boolean): void {
    const next = new Set(this.scopes);
    if (on) next.add(scope);
    else next.delete(scope);
    this.scopes = [...next];
  }

  private submit(): Promise<void> {
    const grantee = this.grantees.find((g) => g.key === this.granteeKey);
    if (!grantee || this.scopes.length === 0) {
      this.error = 'Pick who to share with and at least one scope.';
      return Promise.resolve();
    }
    return this.act(async () => {
      await createRunnerShare(this.runnerId, {
        grantee_type: grantee.type,
        grantee_id: grantee.id,
        scopes: this.scopes,
        harnesses: parseIds(this.harnesses),
        directory_ids: parseIds(this.directories),
      });
      this.granteeKey = '';
      this.harnesses = '';
      this.directories = '';
    });
  }

  private renderForm() {
    if (this.peopleUnavailable) {
      return html`<div class="meta" data-testid="share-form-unavailable">
        Your role cannot list users or teams, so it cannot pick who to share
        with. Ask an account admin to share this runner.
      </div>`;
    }
    return html`<div class="form" data-testid="share-form">
      <sl-select
        size="small"
        label="Share with"
        data-testid="grantee"
        .value=${this.granteeKey}
        @sl-change=${(e: Event) =>
          (this.granteeKey = (e.target as HTMLSelectElement).value)}
      >
        ${this.grantees.map(
          (g) => html`<sl-option value=${g.key}>${g.label}</sl-option>`
        )}
      </sl-select>
      ${(['sessions', 'flows'] as ShareScope[]).map(
        (scope) =>
          html`<sl-checkbox
            size="small"
            data-testid=${`scope-${scope}`}
            ?checked=${this.scopes.includes(scope)}
            @sl-change=${(e: Event) =>
              this.toggleScope(scope, (e.target as HTMLInputElement).checked)}
            >${scope === 'sessions' ? 'Sessions' : 'Flows'}</sl-checkbox
          >`
      )}
      <sl-input
        size="small"
        label="Harnesses (blank: all)"
        placeholder="claude_code"
        data-testid="harnesses"
        .value=${this.harnesses}
        @sl-input=${(e: Event) =>
          (this.harnesses = (e.target as HTMLInputElement).value)}
      ></sl-input>
      <sl-input
        size="small"
        label="Directory ids (blank: all)"
        data-testid="directories"
        .value=${this.directories}
        @sl-input=${(e: Event) =>
          (this.directories = (e.target as HTMLInputElement).value)}
      ></sl-input>
      <sl-button
        size="small"
        variant="primary"
        data-testid="share-submit"
        ?disabled=${this.busy}
        @click=${() => this.submit()}
        >Share</sl-button
      >
    </div>`;
  }

  render() {
    const runners = this.runners();
    if (runners.length === 0 || this.off) return nothing;
    const sharing = this.sharing;
    return html`<h3>Runner sharing</h3>
      <sl-select
        size="small"
        label="Runner"
        data-testid="sharing-runner"
        .value=${this.runnerId}
        @sl-change=${(e: Event) =>
          (this.runnerId = (e.target as HTMLSelectElement).value)}
      >
        ${runners.map((r) => html`<sl-option value=${r.id}>${r.name}</sl-option>`)}
      </sl-select>
      ${
        sharing
          ? html`${this.renderPolicy(sharing)}
            ${
              sharing.grants.length
                ? sharing.grants.map((share) =>
                    this.renderShare(sharing, share)
                  )
                : html`<div class="meta">Not shared.</div>`
            }
            ${sharing.can_share ? this.renderForm() : nothing}`
          : nothing
      }
      ${this.error ? html`<div class="error" role="alert">${this.error}</div>` : nothing}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'runner-sharing-panel': RunnerSharingPanel;
  }
}
