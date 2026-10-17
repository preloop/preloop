import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getFeatures, getUserProfile, hasPermission } from '../api';
import { hasCapability } from '../capabilities';
import {
  getWeeklyReportPreferences,
  updateWeeklyReportPreferences,
  type WeeklyReportPreferences,
} from '../governance-api';
import { showToast } from './confirm-dialog';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';

type Field = Exclude<keyof WeeklyReportPreferences, 'user_enabled'>;

const SWITCHES: {
  field: Field;
  label: string;
  on: string;
  off: string;
  toastOn: string;
  toastOff: string;
}[] = [
  {
    field: 'account_enabled',
    label: 'Report for owners and admins',
    on: 'The full account report: usage, spend, approvals, policy findings and the change from last week. Sent each Monday.',
    off: 'Owners and admins do not get the weekly report.',
    toastOn: 'Owners and admins get the weekly report.',
    toastOff: 'Owners and admins no longer get the weekly report.',
  },
  {
    field: 'members_enabled',
    label: 'Personal reports for members',
    on: "Each member gets their own agents, flows, sessions, spend and approvals, with the team median beside them. Never anyone else's name or numbers.",
    off: 'Members get no weekly report. Owners and admins are not affected.',
    toastOn: 'Members get a personal weekly report.',
    toastOff: 'Members no longer get a weekly report.',
  },
  {
    field: 'member_policy_findings_enabled',
    label: 'Include policy findings in member reports',
    on: 'A member sees the AI usage policy findings about their own sessions, and only those.',
    off: 'Member reports leave out policy findings.',
    toastOn: 'Member reports include their own policy findings.',
    toastOff: 'Member reports leave out policy findings.',
  },
];

/** Account-level weekly report switches, under Account settings. */
@customElement('weekly-report-settings')
export class WeeklyReportSettings extends LitElement {
  @state() private prefs: WeeklyReportPreferences | null = null;
  @state() private canManage = false;
  @state() private switching = '';
  private generation = 0;

  static styles = css`
    :host {
      display: block;
      margin-bottom: 2rem;
    }
    section {
      padding: 1.25rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 0.5rem;
    }
    h2 {
      margin: 0 0 0.25rem;
      font-size: 1.25rem;
    }
    .row {
      padding: 0.75rem 0;
      border-top: 1px solid var(--sl-color-neutral-200);
    }
    .row:first-of-type {
      border-top: none;
    }
    .meta {
      margin: 0.25rem 0 0 3rem;
      font-size: 13px;
      color: var(--sl-color-neutral-600);
      line-height: 1.5;
    }
    p {
      margin: 0 0 0.75rem;
      font-size: 13px;
      color: var(--sl-color-neutral-600);
      line-height: 1.5;
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  disconnectedCallback(): void {
    ++this.generation;
    this.prefs = null;
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    try {
      const features = await getFeatures();
      if (
        generation !== this.generation ||
        !hasCapability(features.features, 'governance_review')
      )
        return;
      const [user, prefs] = await Promise.all([
        getUserProfile(),
        getWeeklyReportPreferences(),
      ]);
      if (generation !== this.generation) return;
      this.canManage = hasPermission(user.permissions, 'manage_account');
      this.prefs = prefs;
    } catch {
      // A server without weekly reports simply shows no card.
    }
  }

  private async toggle(field: Field, on: boolean): Promise<void> {
    if (this.switching) return;
    const spec = SWITCHES.find((s) => s.field === field)!;
    this.switching = field;
    try {
      this.prefs = await updateWeeklyReportPreferences({ [field]: on });
      showToast(on ? spec.toastOn : spec.toastOff, 'success');
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Could not change the report',
        'danger'
      );
      this.requestUpdate();
    } finally {
      this.switching = '';
    }
  }

  render() {
    const prefs = this.prefs;
    if (!prefs) return nothing;
    // Older servers return only account_enabled; show what they support.
    const shown = SWITCHES.filter((s) => prefs[s.field] !== undefined);
    return html`<section aria-labelledby="weekly-report-title">
      <h2 id="weekly-report-title">Weekly report</h2>
      <p>
        Built from usage the account already records, so sending it costs no AI.
        Anyone can stop their own copy from the link in the email.
        ${this.canManage ? '' : 'Owners and admins can change these.'}
      </p>
      ${shown.map((spec) => {
        const on = !!prefs[spec.field];
        const blocked =
          spec.field === 'member_policy_findings_enabled' &&
          !prefs.members_enabled;
        return html`<div class="row">
          <sl-switch
            data-field=${spec.field}
            ?checked=${on}
            ?disabled=${!this.canManage || blocked || !!this.switching}
            @sl-change=${(event: Event) =>
              void this.toggle(
                spec.field,
                (event.target as HTMLInputElement).checked
              )}
            >${spec.label}</sl-switch
          >
          <div class="meta">
            ${blocked ? 'Turn on personal reports for members first.' : on ? spec.on : spec.off}
          </div>
        </div>`;
      })}
    </section>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'weekly-report-settings': WeeklyReportSettings;
  }
}
