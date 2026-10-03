import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import * as api from '../api';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';
import type SlTreeItem from '@shoelace-style/shoelace/dist/components/tree-item/tree-item.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/tree/tree.js';
import '@shoelace-style/shoelace/dist/components/tree-item/tree-item.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import { consoleDialogStyles } from '../styles/console-dialog';
import type { Tracker } from './tracker-item.ts';
import { groupProjectsByGroup } from '../utils/tracker-scope';

const BITBUCKET_WEB_URL = 'https://bitbucket.org';

/**
 * The only Bitbucket Data Center release the adapter is validated against.
 * Other releases are reported by the backend as unsupported or unvalidated;
 * they never fall back to Bitbucket Cloud behaviour.
 */
export const BITBUCKET_DC_VERSION = '10.2';

/**
 * Result of normalising a user-entered Bitbucket Data Center URL.
 * `url` is the canonical `https://host[:port][/context]` origin with no
 * trailing slash; `error` explains why the input was rejected.
 */
export interface BitbucketDcUrlResult {
  url: string | null;
  error: string | null;
}

/**
 * Normalise a Bitbucket Data Center instance URL to its canonical form.
 *
 * This is early client-side feedback only. The backend applies the deployment
 * policy (administrator-approved origins, ports and context paths, DNS and
 * redirect checks); nothing accepted here is treated as approved.
 *
 * Rejected: non-https schemes, userinfo, query strings, fragments and path
 * traversal segments. Trailing slashes are dropped and the host is lowercased
 * so the same instance always serialises to the same string.
 */
export function canonicalizeBitbucketDcUrl(raw: string): BitbucketDcUrlResult {
  const trimmed = (raw ?? '').trim();
  if (!trimmed) {
    return {
      url: null,
      error: 'Enter the Bitbucket Data Center instance URL.',
    };
  }
  // Reject encoded paths and backslashes before the URL parser can normalise
  // them into a different administrator-approved context path.
  if (/[\\%\u0000-\u0020\u007f]/.test(trimmed)) {
    return {
      url: null,
      error:
        'Use an unencoded HTTPS instance URL without spaces or backslashes.',
    };
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return {
      url: null,
      error:
        'Enter a full instance URL such as https://bitbucket.example.com or https://bitbucket.example.com/stash.',
    };
  }
  if (parsed.protocol !== 'https:') {
    return {
      url: null,
      error: 'Bitbucket Data Center instances must use https://.',
    };
  }
  if (parsed.username || parsed.password) {
    return {
      url: null,
      error:
        'Remove credentials from the instance URL; the personal access token is entered separately.',
    };
  }
  if (parsed.search || trimmed.includes('?')) {
    return {
      url: null,
      error: 'The instance URL must not contain a query string.',
    };
  }
  if (parsed.hash || trimmed.includes('#')) {
    return {
      url: null,
      error: 'The instance URL must not contain a fragment.',
    };
  }
  // Compare the raw path so URL's own dot-segment normalisation cannot hide
  // a traversal attempt such as /stash/../admin.
  const rawPath = trimmed.slice(parsed.origin.length);
  const rawSegments = rawPath.split('/').filter((segment) => segment !== '');
  if (rawSegments.some((segment) => segment === '.' || segment === '..')) {
    return {
      url: null,
      error: 'The instance URL must not contain "." or ".." path segments.',
    };
  }
  const segments = parsed.pathname
    .split('/')
    .filter((segment) => segment !== '');
  const contextPath = segments.length ? `/${segments.join('/')}` : '';
  return { url: `${parsed.origin}${contextPath}`, error: null };
}

/** Connection details persisted for a Bitbucket Data Center tracker. */
export type BitbucketDcConnectionDetails = {
  instance_url: string;
  version: string;
  project_key?: string;
  repository_id?: number;
  repository_slug?: string;
  username?: string;
};

@customElement('add-tracker-modal')
export class AddTrackerModal extends LitElement {
  @property({ type: Object })
  tracker: any = null;

  /**
   * Trackers already on this account, when the parent view has them.
   * Used to annotate installations that already back a tracker. Does not
   * hide options or block creating a second tracker on the same installation.
   */
  @property({ type: Array })
  existingTrackers: Tracker[] = [];

  /**
   * @internal
   */
  _api = api;

  @property({ type: Boolean })
  opened = true;

  @state()
  private step = 1;

  @state()
  private trackerName = '';

  @state()
  private trackerType = 'github';

  @state()
  private trackerUrl = 'https://api.github.com';

  @state()
  private trackerToken = '';

  @state()
  private trackerUsername = '';

  /** Bitbucket: `api_token` (API or access token) or `oauth_token`. */
  @state()
  private bitbucketAuthType: 'api_token' | 'oauth_token' = 'api_token';

  /** Bitbucket: personal API token or repository access token. */
  @state()
  private bitbucketTokenKind: 'api_token' | 'access_token' = 'api_token';

  @state()
  private bitbucketWorkspace = '';

  @state()
  private bitbucketRepository = '';

  @state()
  private bitbucketEmail = '';

  /** ISO date (YYYY-MM-DD) the token expires on, if known. */
  @state()
  private tokenExpiresAt = '';

  /**
   * Whether the deployment exposes the opt-in `bitbucket_dc` capability
   * (`features.bitbucket_dc` from `/features`). Off by default; the Data
   * Center option is hidden until the backend reports it enabled.
   */
  @state()
  private bitbucketDcEnabled = false;

  /** Set once the /features lookup has settled, so the gate is not applied on a stale default. */
  @state()
  private bitbucketDcFeatureLoaded = false;

  /** Bitbucket Data Center: instance URL as typed (canonicalised on submit). */
  @state()
  private bitbucketDcInstanceUrl = '';

  /** Bitbucket Data Center: optional project key for manual discovery. */
  @state()
  private bitbucketDcProjectKey = '';

  /** Bitbucket Data Center: optional immutable numeric repository id. */
  @state()
  private bitbucketDcRepositoryId = '';

  /** Bitbucket Data Center: optional repository slug (may change on rename). */
  @state()
  private bitbucketDcRepositorySlug = '';

  @state()
  private orgs: any[] = [];

  @state()
  private projects: Record<string, any[]> = {};

  @state()
  private selectedOrgs: Record<string, boolean> = {};

  @state()
  private selectedProjects: Record<string, Record<string, boolean>> = {};

  @state()
  private areAllProjectsSelected = false;

  @state()
  private includeFutureProjects = true;

  @state()
  private isLoading = false;

  @state()
  private errorMessage = '';

  @state()
  private warningMessages: string[] = [];

  @state()
  private githubAppConfigured = false;

  @state()
  private authMethod: 'api_token' | 'github_app' = 'api_token';

  /**
   * GitHub App installations already registered on this account. Offered as
   * a picker when adding a tracker so an App that is already installed on the
   * target account (GitHub shows its Configure page and never calls our setup
   * callback) can still be bound.
   */
  @state()
  private installations: api.GitHubInstallation[] = [];

  // Properties passed from trackers-view after GitHub OAuth callback
  @property({ type: String })
  githubInstallationId: string | null = null;

  @property({ type: String })
  githubTargetLogin: string | null = null;

  static styles = [
    consoleDialogStyles,
    css`
      .error {
        color: var(--sl-color-danger-700);
      }
      sl-input,
      sl-select {
        margin-bottom: 1rem;
      }
      .select-all {
        margin-bottom: 1rem;
        margin-left: 0.5rem;
      }
      .include-future {
        margin-left: 0.5rem;
        margin-top: 1rem;
      }
      .project-group {
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-600);
        margin: 0.5rem 0 0.25rem 1.5rem;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    if (this.tracker) {
      this.trackerName = this.tracker.name;
      this.trackerType = this.tracker.tracker_type;
      this.trackerUrl = this.tracker.url;
      this.trackerToken = 'unchanged';
      this.trackerUsername = this.tracker.connection_details?.username;
      if (this.tracker.tracker_type === 'bitbucket') {
        const details = this.tracker.connection_details ?? {};
        this.bitbucketAuthType =
          this.tracker.auth_type === 'oauth_token'
            ? 'oauth_token'
            : 'api_token';
        this.bitbucketTokenKind =
          details.token_kind === 'access_token' ? 'access_token' : 'api_token';
        this.bitbucketWorkspace = details.workspace ?? '';
        this.bitbucketRepository = details.repository ?? '';
        this.bitbucketEmail = details.email ?? '';
        this.tokenExpiresAt = details.token_expires_at ?? '';
      }
      if (this.tracker.tracker_type === 'bitbucket_dc') {
        // instance_url is the canonical origin plus context path; it is the
        // identity the stored PAT is bound to, so prefer it over `url`.
        const details = this.tracker.connection_details ?? {};
        this.bitbucketDcInstanceUrl =
          details.instance_url ?? this.tracker.url ?? '';
        this.trackerUrl = this.bitbucketDcInstanceUrl;
        this.bitbucketDcProjectKey = details.project_key ?? '';
        this.bitbucketDcRepositoryId =
          details.repository_id === undefined || details.repository_id === null
            ? ''
            : String(details.repository_id);
        this.bitbucketDcRepositorySlug = details.repository_slug ?? '';
      }
      this.authMethod = this.isOAuthAuthType(this.tracker.auth_type)
        ? 'github_app'
        : 'api_token';
      // An existing App tracker keeps its installation binding; the backend
      // resolves it from the tracker id, so no installation id is needed here
      // (oauth_installation_id is our internal UUID, not GitHub's id).
      if (this.authMethod === 'github_app') {
        this.githubTargetLogin =
          this.tracker.github_installation_target_login ?? null;
      }
      this.selectedOrgs = this.tracker.scope_rules
        .filter(
          (x: any) => x.rule_type == 'INCLUDE' && x.scope_type == 'ORGANIZATION'
        )
        .reduce((acc: any, x: any) => {
          acc[x.identifier] = true;
          return acc;
        }, {});
      this.selectedProjects = {};
      // Object.keys(this.selectedOrgs).forEach((orgId: any) => {
      //   this.selectedProjects[orgId] = {};
      //   this.projects[orgId]?.forEach((project: any) => {
      //     if (this.tracker.scope_rules.filter((x: any) => x.scope_type == 'PROJECT' && x.rule_type == 'INCLUDE' && x.identifier == project.id).length ||
      //         !this.tracker.scope_rules.filter((x: any) => x.scope_type == 'PROJECT' && x.rule_type == 'EXCLUDE' && x.identifier == project.id).length) {
      //       this.selectedProjects[orgId][project.id] = true;
      //     }
      //   });
      // });
      this.includeFutureProjects = !this.tracker.scope_rules.filter(
        (x: any) => x.rule_type == 'INCLUDE' && x.scope_type == 'PROJECT'
      ).length;
      // Token is not pre-filled for security reasons
      if (this.tracker) {
        this.trackerToken = 'unchanged';
      }
    }
    // Check if GitHub App OAuth is available
    this.checkGitHubAppAvailability();
    // Check whether this deployment opted in to Bitbucket Data Center
    this.checkBitbucketDcAvailability();

    // If we have a GitHub installation ID from OAuth callback (not editing), set up for GitHub App flow
    // Only apply these defaults for new trackers, not when editing existing ones
    if (this.githubInstallationId && !this.tracker) {
      this.trackerType = 'github';
      this.authMethod = 'github_app';
      this.trackerUrl = 'https://github.com';
      if (this.githubTargetLogin) {
        this.trackerName = `GitHub - ${this.githubTargetLogin}`;
      }
      // Skip auth method selection, go directly to step 1
      this.step = 1;
    }
  }

  private isOAuthAuthType(authType: string | undefined | null): boolean {
    return authType === 'github_app' || authType === 'oauth_app';
  }

  private get isEditingAppTracker(): boolean {
    return !!this.tracker && this.authMethod === 'github_app';
  }

  private get isBitbucketDc(): boolean {
    return this.trackerType === 'bitbucket_dc';
  }

  /**
   * An existing Data Center tracker is being edited on a deployment that has
   * the capability switched off. The form keeps its real type (never a silent
   * Cloud fallback) and blocks submission until an administrator enables it.
   */
  private get isBitbucketDcEditBlocked(): boolean {
    return (
      !!this.tracker &&
      this.tracker.tracker_type === 'bitbucket_dc' &&
      this.bitbucketDcFeatureLoaded &&
      !this.bitbucketDcEnabled
    );
  }

  async checkBitbucketDcAvailability() {
    try {
      const response = await this._api.getFeatures();
      this.bitbucketDcEnabled = response.features?.['bitbucket_dc'] === true;
    } catch (error) {
      console.error(
        'Failed to check Bitbucket Data Center availability:',
        error
      );
      this.bitbucketDcEnabled = false;
    } finally {
      this.bitbucketDcFeatureLoaded = true;
    }
  }

  async checkGitHubAppAvailability() {
    try {
      const authMethods = await this._api.getTrackerAuthMethods();
      this.githubAppConfigured = authMethods.github_app_configured;
      // Don't auto-show auth selection on load - user should see the tracker form first
      // Auth selection is triggered when user clicks "Next" with GitHub selected
    } catch (error) {
      console.error('Failed to check GitHub App availability:', error);
      this.githubAppConfigured = false;
    }
    if (this.githubAppConfigured && !this.tracker) {
      await this.loadExistingInstallations();
    }
  }

  private async loadExistingInstallations() {
    try {
      this.installations = await this._api.getGitHubInstallations();
    } catch (error) {
      console.error('Failed to load GitHub App installations:', error);
      this.installations = [];
    }
  }

  private selectExistingInstallation(installationId: string) {
    const installation = this.installations.find(
      (inst) => String(inst.installation_id) === installationId
    );
    if (!installation) {
      return;
    }
    this.errorMessage = '';
    this.trackerType = 'github';
    this.trackerUrl = 'https://github.com';
    this.authMethod = 'github_app';
    this.githubInstallationId = String(installation.installation_id);
    this.githubTargetLogin = installation.target_login;
    if (!this.trackerName) {
      this.trackerName = `GitHub - ${installation.target_login}`;
    }
  }

  private installationAlreadyTracking(
    installation: api.GitHubInstallation
  ): boolean {
    return this.existingTrackers.some(
      (tracker) => tracker.oauth_installation_id === installation.id
    );
  }

  private installationLabel(installation: api.GitHubInstallation): string {
    const label = `${installation.target_login} (${installation.target_type})`;
    if (this.installationAlreadyTracking(installation)) {
      return `${label} (already tracking)`;
    }
    return label;
  }
  firstUpdated() {
    // Reset state when modal is shown
    this.warningMessages = [];
    this.errorMessage = '';
    this.shadowRoot?.querySelector('sl-dialog')?.show();

    // Auto-trigger tracker creation when coming from GitHub App OAuth callback
    if (this.githubInstallationId && !this.tracker) {
      // Small delay to let the dialog render, then auto-save
      setTimeout(() => this.testConnection(), 200);
    } else {
      setTimeout(() => {
        const input = this.shadowRoot?.querySelector<SlInput>('sl-input');
        input?.focus();
      }, 100);
    }
  }
  render() {
    return html`
      <sl-dialog
        label="${this.tracker ? 'Edit' : 'Add'} Tracker"
        @sl-request-close=${() => this.closeModal()}
      >
        ${this.step === 1 ? this.renderStep1() : this.renderStep2()}
        ${
          this.warningMessages.length > 0
            ? html`
                <sl-alert variant="warning" open style="margin-top: 1rem;">
                  <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                  <strong>Warnings:</strong>
                  <ul style="margin: 0.5rem 0 0 0; padding-left: 1.5rem;">
                    ${this.warningMessages.map((w) => html`<li>${w}</li>`)}
                  </ul>
                </sl-alert>
              `
            : ''
        }
        ${
          this.errorMessage
            ? html`<p class="error">${this.errorMessage}</p>`
            : ''
        }
        <div slot="footer">${this.renderFooterButtons()}</div>
      </sl-dialog>
    `;
  }

  renderFooterButtons() {
    // Show "Done" button after successful save with warnings
    if (this.warningMessages.length > 0) {
      return html`
        <sl-button variant="primary" @click=${() => this.closeModal(true)}>
          Done
        </sl-button>
      `;
    }
    if (this.step === 1) {
      return html`
        <sl-button @click=${() => this.closeModal()}>Cancel</sl-button>
        <sl-button
          variant="primary"
          @click=${this.testConnection}
          .loading=${this.isLoading}
          ?disabled=${this.isBitbucketDcEditBlocked}
          >Next</sl-button
        >
      `;
    }
    return html`
      <sl-button @click=${() => (this.step = 1)}>Back</sl-button>
      <sl-button
        variant="primary"
        @click=${this.handleSave}
        .loading=${this.isLoading}
      >
        ${this.tracker ? 'Save' : 'Add'}
      </sl-button>
    `;
  }

  async startGitHubOAuth() {
    this.isLoading = true;
    this.errorMessage = '';
    try {
      this.dispatchEvent(
        new CustomEvent('github-oauth-starting', {
          bubbles: true,
          composed: true,
        })
      );
      sessionStorage.setItem(
        'github_oauth_redirect_back',
        window.location.pathname + window.location.search
      );

      const { authorization_url, state } = await this._api.getGitHubAuthUrl();
      // Store state for CSRF validation on callback
      sessionStorage.setItem('github_oauth_state', state);
      // Redirect to GitHub
      window.location.href = authorization_url;
    } catch (error: any) {
      this.errorMessage = error.message || 'Failed to start GitHub OAuth';
      this.authMethod = 'api_token'; // Fall back to API token
    } finally {
      this.isLoading = false;
    }
  }

  renderStep1() {
    return html`
      <sl-input
        label="Name"
        name="name"
        .value=${this.trackerName}
        @sl-input=${(e: any) => (this.trackerName = e.target.value)}
        required
        tabindex="0"
        autofocus
      ></sl-input>
      <sl-select
        label="Type"
        name="type"
        .value=${this.trackerType}
        ?disabled=${this.isBitbucketDcEditBlocked}
        @sl-change=${(e: any) => {
          this.trackerType = e.target.value;
          const urlInput = this.shadowRoot?.querySelector(
            'sl-input[name="url"]'
          ) as HTMLInputElement;
          if (this.trackerType === 'bitbucket') {
            this.trackerUrl = BITBUCKET_WEB_URL;
          } else if (this.trackerType === 'bitbucket_dc') {
            // The canonical instance URL is derived from the DC field on
            // submit; keep the shared url state in step with it.
            this.trackerUrl = this.bitbucketDcInstanceUrl;
          } else if (this.trackerType === 'gitlab') {
            this.trackerUrl = 'https://gitlab.com';
            if (urlInput) {
              urlInput.placeholder = 'e.g., https://gitlab.example.com';
            }
          } else if (this.trackerType === 'github') {
            this.trackerUrl = 'https://github.com';
            if (urlInput) {
              urlInput.placeholder = 'e.g., https://github.example.com';
            }
            // Auth method selection is shown when clicking "Next" via testConnection
          } else {
            this.trackerUrl = '';
            if (urlInput) {
              urlInput.placeholder = 'e.g., https://your-team.atlassian.net';
            }
          }
        }}
      >
        <sl-option value="github">GitHub</sl-option>
        <sl-option value="gitlab">GitLab</sl-option>
        <sl-option value="jira">Jira</sl-option>
        <sl-option value="bitbucket">Bitbucket Cloud</sl-option>
        ${
          this.bitbucketDcEnabled ||
          this.tracker?.tracker_type === 'bitbucket_dc'
            ? html`
                <sl-option value="bitbucket_dc"
                  >Bitbucket Data Center</sl-option
                >
              `
            : ''
        }
      </sl-select>
      ${
        this.isBitbucketDcEditBlocked
          ? html`
              <sl-alert variant="warning" open class="bitbucket-dc-disabled">
                <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                <strong
                  >Bitbucket Data Center is disabled on this deployment.</strong
                >
                This tracker keeps its Data Center configuration and stored
                token, but it cannot be tested or saved until an administrator
                enables the <code>bitbucket_dc</code> capability. It is not
                treated as a Bitbucket Cloud tracker.
              </sl-alert>
            `
          : ''
      }
      ${
        this.trackerType === 'bitbucket'
          ? this.renderBitbucketFields()
          : this.isBitbucketDc
            ? this.renderBitbucketDcFields()
            : html`
                <sl-input
                  label="URL"
                  name="url"
                  .value=${this.trackerUrl}
                  @sl-input=${(e: any) => (this.trackerUrl = e.target.value)}
                  placeholder="e.g., https://github.example.com"
                ></sl-input>
              `
      }
      ${
        this.trackerType === 'jira'
          ? html`
              <sl-input
                label="Jira Username"
                name="username"
                .value=${this.trackerUsername}
                @sl-input=${(e: any) => (this.trackerUsername = e.target.value)}
                required
              ></sl-input>
            `
          : ''
      }
      ${
        this.isEditingAppTracker
          ? html`
              <sl-alert variant="neutral" open>
                <sl-icon slot="icon" name="github"></sl-icon>
                Authenticates through the GitHub App installation
                ${
                  this.githubTargetLogin
                    ? html`for <strong>${this.githubTargetLogin}</strong>`
                    : ''
                }.
                No API token is needed.
              </sl-alert>
            `
          : this.authMethod === 'github_app' && this.githubInstallationId
            ? html`
                <sl-alert variant="success" open>
                  <sl-icon slot="icon" name="check-circle"></sl-icon>
                  Connected to GitHub as
                  <strong>${this.githubTargetLogin}</strong>
                </sl-alert>
              `
            : this.trackerType === 'github' &&
                this.githubAppConfigured &&
                !this.tracker
              ? html`
                  <div style="margin-bottom: 1rem;">
                    <sl-button
                      variant="primary"
                      size="large"
                      @click=${this.startGitHubOAuth}
                      .loading=${this.isLoading}
                      style="width: 100%;"
                    >
                      <sl-icon slot="prefix" name="github"></sl-icon>
                      Connect with GitHub
                    </sl-button>
                    <p
                      style="text-align: center; margin: 0.75rem 0 0.5rem 0; color: var(--sl-color-neutral-500); font-size: var(--sl-font-size-small);"
                    >
                      Recommended: One-click OAuth connection
                    </p>
                    ${
                      this.installations.length > 0
                        ? html`
                            <sl-select
                              label="Use an existing installation"
                              name="installation"
                              placeholder="Select an installation"
                              help-text="The GitHub App is already installed on these accounts."
                              @sl-change=${(e: any) =>
                                this.selectExistingInstallation(
                                  String(e.target.value)
                                )}
                            >
                              ${this.installations.map(
                                (inst) => html`
                                  <sl-option value="${inst.installation_id}">
                                    ${this.installationLabel(inst)}
                                  </sl-option>
                                `
                              )}
                            </sl-select>
                          `
                        : ''
                    }
                  </div>
                  <details style="margin-bottom: 1rem;">
                    <summary
                      style="cursor: pointer; color: var(--sl-color-neutral-600); font-size: var(--sl-font-size-small);"
                    >
                      Or use an API token instead
                    </summary>
                    <sl-input
                      type="password"
                      label="API Key"
                      name="api_key"
                      .value=${this.trackerToken}
                      @sl-input=${(e: any) => (this.trackerToken = e.target.value)}
                      style="margin-top: 0.5rem;"
                    ></sl-input>
                  </details>
                `
              : this.isBitbucketDc
                ? ''
                : html`
                    <sl-input
                      type="password"
                      label="API Key"
                      name="api_key"
                      .value=${this.trackerToken}
                      @sl-input=${(e: any) =>
                        (this.trackerToken = e.target.value)}
                      required
                    ></sl-input>
                  `
      }
    `;
  }

  /**
   * Bitbucket Data Center settings: canonical instance URL, PAT and optional
   * project/repository hints for manual discovery.
   */
  renderBitbucketDcFields() {
    const blocked = this.isBitbucketDcEditBlocked;
    return html`
      <sl-alert variant="neutral" open class="bitbucket-dc-notice">
        <sl-icon slot="icon" name="info-circle"></sl-icon>
        <p style="margin: 0 0 0.5rem 0;">
          <strong>Administrator approval required.</strong> Preloop only
          connects to Bitbucket Data Center instances whose origin, port and
          context path an administrator has approved for this deployment.
          Private-network instances also need an explicit allowlist entry.
          Unapproved instances are rejected when you continue.
        </p>
        <p style="margin: 0;">
          <strong
            >Fixture-tested against Data Center ${BITBUCKET_DC_VERSION}
            LTS</strong
          >
          (REST 1.0); live certification is separate. Other releases are
          reported as unsupported or unvalidated and operations the release does
          not support are declined, never faked and never routed through
          Bitbucket Cloud behaviour.
        </p>
      </sl-alert>
      <sl-input
        label="Instance URL"
        name="url"
        .value=${this.bitbucketDcInstanceUrl}
        ?disabled=${!!this.tracker}
        @sl-input=${(e: any) => {
          this.bitbucketDcInstanceUrl = e.target.value;
          this.trackerUrl = e.target.value;
        }}
        placeholder="https://bitbucket.example.com/stash"
        help-text=${
          this.tracker
            ? 'The stored token is bound to this instance. Create a new tracker to connect a different instance.'
            : 'https origin plus context path, if any. No credentials, query string or fragment.'
        }
        required
      ></sl-input>
      <sl-input
        type="password"
        label="Personal access token"
        name="api_key"
        .value=${this.trackerToken}
        ?disabled=${blocked}
        @sl-input=${(e: any) => (this.trackerToken = e.target.value)}
        help-text=${
          this.tracker
            ? 'Leave as is to keep the stored token. Enter a new token to replace it.'
            : 'A Bitbucket Data Center personal access token with repository read and write permissions. Stored encrypted; never written to logs.'
        }
        password-toggle
        required
      ></sl-input>
      <sl-input
        label="Reviewer user slug"
        name="bitbucket_dc_username"
        .value=${this.trackerUsername ?? ''}
        ?disabled=${blocked}
        @sl-input=${(e: any) => (this.trackerUsername = e.target.value)}
        help-text="Optional. Your Data Center user slug for review verdicts when the server does not identify the current user in its response. The PAT still uses Bearer authentication."
      ></sl-input>
      <sl-input
        label="Project key"
        name="bitbucket_dc_project_key"
        .value=${this.bitbucketDcProjectKey}
        ?disabled=${blocked}
        @sl-input=${(e: any) => (this.bitbucketDcProjectKey = e.target.value)}
        help-text="Optional. Limit discovery to one Data Center project key."
      ></sl-input>
      <sl-input
        label="Repository ID"
        name="bitbucket_dc_repository_id"
        type="number"
        min="1"
        step="1"
        .value=${this.bitbucketDcRepositoryId}
        ?disabled=${blocked}
        @sl-input=${(e: any) => (this.bitbucketDcRepositoryId = e.target.value)}
        help-text="Optional. The numeric repository id stays stable when the repository is renamed."
      ></sl-input>
      <sl-input
        label="Repository slug"
        name="bitbucket_dc_repository_slug"
        .value=${this.bitbucketDcRepositorySlug}
        ?disabled=${blocked}
        @sl-input=${(e: any) =>
          (this.bitbucketDcRepositorySlug = e.target.value)}
        help-text="Optional. The current repository slug; discovery resolves it against the repository id."
      ></sl-input>
    `;
  }

  /**
   * Connection details sent for a Bitbucket Data Center tracker. The instance
   * URL is canonical (https origin plus context path, no trailing slash) and
   * optional discovery hints are only included when set.
   *
   * @throws Error when the instance URL or repository id is invalid.
   */
  bitbucketDcConnectionDetails(): BitbucketDcConnectionDetails {
    const { url, error } = canonicalizeBitbucketDcUrl(
      this.bitbucketDcInstanceUrl
    );
    if (!url) {
      throw new Error(error ?? 'Invalid Bitbucket Data Center instance URL.');
    }
    const details: BitbucketDcConnectionDetails = {
      instance_url: url,
      version: BITBUCKET_DC_VERSION,
    };
    const reviewer = (this.trackerUsername ?? '').trim();
    if (reviewer) {
      details.username = reviewer;
    }
    const projectKey = this.bitbucketDcProjectKey.trim();
    if (projectKey) {
      details.project_key = projectKey;
    }
    const repositoryId = this.bitbucketDcRepositoryId.trim();
    if (repositoryId) {
      if (
        !/^\d+$/.test(repositoryId) ||
        !Number.isSafeInteger(Number(repositoryId)) ||
        Number(repositoryId) <= 0
      ) {
        throw new Error('Repository ID must be a positive whole number.');
      }
      details.repository_id = Number(repositoryId);
    }
    const repositorySlug = this.bitbucketDcRepositorySlug.trim();
    if (repositorySlug) {
      details.repository_slug = repositorySlug;
    }
    return details;
  }

  /** Bitbucket Cloud settings: workspace, token kind and git identity. */
  renderBitbucketFields() {
    const isAccessToken =
      this.bitbucketAuthType === 'api_token' &&
      this.bitbucketTokenKind === 'access_token';
    return html`
      <sl-input
        label="Workspace"
        name="bitbucket_workspace"
        .value=${this.bitbucketWorkspace}
        @sl-input=${(e: any) => (this.bitbucketWorkspace = e.target.value)}
        help-text="The workspace ID from bitbucket.org/<workspace>."
        required
      ></sl-input>
      <sl-select
        label="Authentication"
        name="bitbucket_auth_type"
        .value=${this.bitbucketAuthType}
        ?disabled=${!!this.tracker}
        @sl-change=${(e: any) => (this.bitbucketAuthType = e.target.value)}
      >
        <sl-option value="api_token">API token or access token</sl-option>
        <sl-option value="oauth_token">OAuth access token</sl-option>
      </sl-select>
      ${
        this.bitbucketAuthType === 'api_token'
          ? html`
              <sl-select
                label="Token kind"
                name="bitbucket_token_kind"
                .value=${this.bitbucketTokenKind}
                @sl-change=${(e: any) =>
                  (this.bitbucketTokenKind = e.target.value)}
                help-text="App passwords are not accepted. Create an API token with Bitbucket scopes instead."
              >
                <sl-option value="api_token">Personal API token</sl-option>
                <sl-option value="access_token"
                  >Repository access token</sl-option
                >
              </sl-select>
            `
          : ''
      }
      <sl-input
        label="Repository"
        name="bitbucket_repository"
        .value=${this.bitbucketRepository}
        @sl-input=${(e: any) => (this.bitbucketRepository = e.target.value)}
        help-text=${
          isAccessToken
            ? 'Required: a repository access token works on one repository.'
            : 'Optional: limit the tracker to one repository slug.'
        }
        ?required=${isAccessToken}
      ></sl-input>
      ${
        this.bitbucketAuthType === 'api_token' && !isAccessToken
          ? html`
              <sl-input
                label="Atlassian account email"
                name="bitbucket_email"
                type="email"
                .value=${this.bitbucketEmail}
                @sl-input=${(e: any) => (this.bitbucketEmail = e.target.value)}
                help-text="Used only for the REST API Basic auth fallback. Never used for git."
              ></sl-input>
              <sl-input
                label="Bitbucket username"
                name="bitbucket_username"
                .value=${this.trackerUsername ?? ''}
                @sl-input=${(e: any) => (this.trackerUsername = e.target.value)}
                help-text="Used as the git username for clones and pushes. Leave empty to use x-bitbucket-api-token-auth."
              ></sl-input>
            `
          : ''
      }
      <sl-input
        label="Token expires on"
        name="token_expires_at"
        type="date"
        .value=${this.tokenExpiresAt}
        @sl-input=${(e: any) => (this.tokenExpiresAt = e.target.value)}
        help-text="Optional. Preloop warns 14 days before the token expires."
      ></sl-input>
    `;
  }

  /** Connection details sent for a Bitbucket tracker. */
  bitbucketConnectionDetails(): Record<string, string> {
    const details: Record<string, string> = {
      workspace: this.bitbucketWorkspace.trim(),
    };
    if (this.bitbucketAuthType === 'api_token') {
      details.token_kind = this.bitbucketTokenKind;
    }
    if (this.bitbucketRepository.trim()) {
      details.repository = this.bitbucketRepository.trim();
    }
    const usesPersonalToken =
      this.bitbucketAuthType === 'api_token' &&
      this.bitbucketTokenKind === 'api_token';
    if (usesPersonalToken && this.bitbucketEmail.trim()) {
      details.email = this.bitbucketEmail.trim();
    }
    if (usesPersonalToken && this.trackerUsername?.trim()) {
      details.username = this.trackerUsername.trim();
    }
    if (this.tokenExpiresAt) {
      details.token_expires_at = this.tokenExpiresAt;
    }
    return details;
  }

  /** Extra options for the test and project-listing endpoints. */
  private connectionOptions(): api.TrackerConnectionOptions {
    if (this.isBitbucketDc) {
      return {
        connectionDetails: this.bitbucketDcConnectionDetails(),
        authType: 'api_token',
      };
    }
    if (this.trackerType !== 'bitbucket') {
      return {};
    }
    return {
      connectionDetails: this.bitbucketConnectionDetails(),
      authType: this.bitbucketAuthType,
    };
  }

  /**
   * Resolve the Data Center form into the canonical instance URL and
   * connection details, surfacing validation problems as the form error.
   * Returns null (with `errorMessage` set) when the form cannot be submitted.
   */
  private prepareBitbucketDc(): BitbucketDcConnectionDetails | null {
    if (this.isBitbucketDcEditBlocked) {
      this.errorMessage =
        'Bitbucket Data Center is disabled on this deployment. Ask an administrator to enable the bitbucket_dc capability before editing this tracker.';
      return null;
    }
    if (!this.tracker && !this.trackerToken.trim()) {
      this.errorMessage = 'Enter a personal access token to continue.';
      return null;
    }
    try {
      const details = this.bitbucketDcConnectionDetails();
      this.trackerUrl = details.instance_url;
      return details;
    } catch (error: any) {
      this.errorMessage = error.message;
      return null;
    }
  }

  /**
   * Render the project items of one organization. Bitbucket repositories are
   * grouped under their Bitbucket project; other trackers stay flat.
   */
  renderProjectItems(orgId: string, projects: any[]) {
    const item = (proj: any) => html`
      <sl-tree-item
        value="${proj.id}"
        ?selected=${this.selectedProjects[orgId]?.[proj.id]}
      >
        ${proj.name}
      </sl-tree-item>
    `;
    const groups = groupProjectsByGroup(projects);
    if (groups.length <= 1 && !groups[0]?.name) {
      return projects.map(item);
    }
    return groups.map(
      (group) => html`
        <div
          class="project-group"
          slot="children"
          data-group=${group.name || 'other'}
        >
          ${group.name || 'No project'}
        </div>
        ${group.projects.map(item)}
      `
    );
  }

  renderStep2() {
    return html`
      <h2>Configure Project Scope</h2>
      <div>
        <sl-checkbox
          .checked=${this.areAllProjectsSelected}
          @sl-change=${this.toggleSelectAll}
          class="select-all"
        >
          Select All
        </sl-checkbox>
      </div>
      ${this.renderOrgTree()}
      <div>
        <sl-checkbox
          .checked=${this.includeFutureProjects}
          @sl-change=${(e: any) =>
            (this.includeFutureProjects = e.target.checked)}
          class="include-future"
        >
          Include future projects
        </sl-checkbox>
      </div>
    `;
  }

  renderOrgTree() {
    // For GitHub App auth, orgs represent installations - no expandable projects
    const isGitHubApp = this.authMethod === 'github_app';

    setTimeout(() => {
      this.shadowRoot
        ?.querySelectorAll('sl-tree-item')
        ?.forEach((item: any) => {
          if (item.selected && !isGitHubApp) {
            item.dispatchEvent(
              new CustomEvent('sl-lazy-load', { bubbles: true })
            );
          }
        });
    }, 300);
    return html`
      <sl-tree
        selection="multiple"
        @sl-selection-change=${this.handleSelectionChange}
      >
        ${this.orgs.map(
          (org: any) => html`
            <sl-tree-item
              value="${org.id}"
              ?selected=${this.selectedOrgs[org.id]}
              ?lazy=${!isGitHubApp && !this.projects[org.id]}
              ?loading=${this.isLoading}
              ?expanded=${!isGitHubApp && this.selectedOrgs[org.id]}
              @sl-lazy-load=${(e: { target: SlTreeItem }) =>
                this.loadProjects(org.id, e.target)}
            >
              ${org.name}
              ${
                !isGitHubApp && this.projects[org.id]
                  ? this.renderProjectItems(org.id, this.projects[org.id])
                  : ''
              }
            </sl-tree-item>
          `
        )}
      </sl-tree>
    `;
  }

  async testConnection() {
    this.isLoading = true;
    this.errorMessage = '';

    // For GitHub with GitHub App configured but no auth chosen yet, show error
    if (
      this.trackerType === 'github' &&
      this.githubAppConfigured &&
      !this.tracker &&
      !this.githubInstallationId &&
      !this.trackerToken
    ) {
      this.isLoading = false;
      this.errorMessage =
        'Please connect with GitHub or enter an API token to continue.';
      return;
    }

    // Data Center: validate the form before the PAT leaves the browser.
    if (this.isBitbucketDc && !this.prepareBitbucketDc()) {
      this.isLoading = false;
      return;
    }

    try {
      if (this.isEditingAppTracker) {
        // Editing an App tracker: the backend resolves the tracker's own
        // installation from the tracker id and lists the owners it can see,
        // so the scope tree only offers ids that match its scope rules.
        const response = await this._api.validateTrackerToken(
          this.trackerType,
          'unchanged',
          this.trackerUrl,
          this.trackerUsername,
          this.tracker.id
        );
        if (!response.success) {
          this.errorMessage = response.message.split('\n')[0];
          return;
        }
        if (!response.orgs || response.orgs.length === 0) {
          // An empty tree would let Save write scope_rules: [] and wipe the
          // tracker's scope. Keep the existing rules and explain instead.
          this.errorMessage =
            'This installation has no accessible repositories; grant the App access on GitHub first.';
          return;
        }
        this.orgs = response.orgs;
        this.step = 2;
        return;
      }

      // For GitHub App auth, complete the installation and save directly
      // Users already select org/repo access during GitHub App installation
      if (this.authMethod === 'github_app' && this.githubInstallationId) {
        // Complete the installation to associate it with the account
        await this._api.completeGitHubInstallation({
          installation_id: this.githubInstallationId,
        });

        // One tracker is bound to one installation: scope it to that
        // installation's owner only, not to every installation on the account.
        const installations = await this._api.getGitHubInstallations();
        const bound = installations.filter(
          (inst) =>
            String(inst.installation_id) === String(this.githubInstallationId)
        );
        if (bound.length === 0) {
          this.errorMessage = `GitHub App installation ${this.githubInstallationId} is not registered on this account.`;
          return;
        }
        // Use target_id (GitHub org/user ID) as the identifier for scope rules
        this.orgs = bound.map((inst) => ({
          id: String(inst.target_id),
          name: inst.target_login,
          type: inst.target_type,
        }));

        // Auto-select the installation owner (user already selected access during GitHub App setup)
        this.selectedOrgs = {};
        for (const org of this.orgs) {
          this.selectedOrgs[org.id] = true;
        }

        await this.handleSave();
      } else {
        // Standard API token flow
        const response = await this._api.validateTrackerToken(
          this.trackerType,
          this.trackerToken,
          this.trackerUrl,
          this.trackerUsername,
          this.tracker?.id,
          this.connectionOptions()
        );
        if (!response.success) {
          this.errorMessage = response.message.split('\n')[0];
          return;
        }
        this.orgs = response.orgs;
        this.step = 2;
      }
    } catch (error: any) {
      this.errorMessage = error.message;
    } finally {
      this.isLoading = false;
    }
  }

  async loadProjects(orgId: string, item?: SlTreeItem) {
    if (this.projects[orgId]) {
      // Projects already loaded
      return;
    }
    // GitHub App auth doesn't support per-project listing - access is at installation level
    if (this.authMethod === 'github_app') {
      return;
    }
    this.isLoading = true;
    if (item) {
      item.loading = true;
    }
    try {
      const projects = await this._api.listProjectsForOrg(
        this.trackerType,
        this.trackerToken,
        orgId,
        this.trackerUrl,
        this.trackerUsername,
        this.tracker?.id,
        this.connectionOptions()
      );
      this.projects = { ...this.projects, [orgId]: projects };
      if (this.selectedOrgs[orgId]) {
        if (!this.includeFutureProjects) {
          this.selectedProjects[orgId] = projects.reduce(
            (acc: Record<string, boolean>, proj: any) => {
              const scopeId = this.projectScopeIdentifier(proj);
              acc[proj.id] =
                this.tracker?.scope_rules.some(
                  (rule: any) =>
                    rule.rule_type === 'INCLUDE' &&
                    rule.scope_type === 'PROJECT' &&
                    String(rule.identifier) === scopeId
                ) ?? false;
              return acc;
            },
            {} as Record<string, boolean>
          );
        } else {
          this.selectedProjects[orgId] = projects.reduce(
            (acc: Record<string, boolean>, proj: any) => {
              const scopeId = this.projectScopeIdentifier(proj);
              acc[proj.id] = !this.tracker?.scope_rules.some(
                (rule: any) =>
                  rule.rule_type === 'EXCLUDE' &&
                  rule.scope_type === 'PROJECT' &&
                  String(rule.identifier) === scopeId
              );
              return acc;
            },
            {} as Record<string, boolean>
          );
        }
      }
    } catch (error: any) {
      this.errorMessage = error.message;
    } finally {
      this.isLoading = false;
      if (item) {
        item.loading = false;
        item.lazy = false;
      }
      this.requestUpdate();
    }
  }

  handleSelectionChange(event: CustomEvent) {
    const selectedItems = event.detail.selection as SlTreeItem[];
    const newSelectedOrgs: Record<string, boolean> = {};
    const newSelectedProjects: Record<string, Record<string, boolean>> = {};
    // Initialize projects map
    this.orgs.forEach((org) => {
      newSelectedProjects[org.id] = {};
    });

    selectedItems.forEach((item) => {
      const value = item.getAttribute('value');
      if (!value) return;
      const org = item.parentElement?.getAttribute('value');
      if (!org) {
        // Top-level item: an organization without project children
        // (GitHub App trackers scope at the installation owner level).
        if (value in newSelectedProjects) {
          newSelectedOrgs[value] = true;
        }
        return;
      }
      newSelectedOrgs[org] = true;
      newSelectedProjects[org][value] = true;
    });

    this.selectedOrgs = newSelectedOrgs;
    this.selectedProjects = newSelectedProjects;
    this.updateSelectAllState();
  }

  async toggleSelectAll() {
    this.areAllProjectsSelected = !this.areAllProjectsSelected;

    if (this.areAllProjectsSelected) {
      this.isLoading = true;
      const promises = this.orgs
        .filter((org) => !this.projects[org.id])
        .map((org) => this.loadProjects(org.id));
      await Promise.all(promises);
      this.isLoading = false;
    }

    const newSelectedOrgs: Record<string, boolean> = {};
    const newSelectedProjects: Record<string, Record<string, boolean>> = {};

    this.orgs.forEach((org) => {
      newSelectedOrgs[org.id] = this.areAllProjectsSelected;
      newSelectedProjects[org.id] = {};
      if (this.projects[org.id]) {
        this.projects[org.id].forEach((proj) => {
          newSelectedProjects[org.id][proj.id] = this.areAllProjectsSelected;
        });
      }
    });

    this.selectedOrgs = newSelectedOrgs;
    this.selectedProjects = newSelectedProjects;
    this.requestUpdate();
  }

  updateSelectAllState() {
    // Only consider loaded projects for the "Select All" state
    const loadedProjects = this.orgs
      .filter((org) => this.projects[org.id])
      .flatMap((org) => this.projects[org.id]);

    if (loadedProjects.length === 0) {
      this.areAllProjectsSelected = false;
      return;
    }

    let allSelected = true;
    for (const org of this.orgs) {
      if (this.projects[org.id]) {
        for (const proj of this.projects[org.id]) {
          if (!this.selectedProjects[org.id]?.[proj.id]) {
            allSelected = false;
            break;
          }
        }
      }
      if (!allSelected) break;
    }
    this.areAllProjectsSelected = allSelected;
  }

  private projectScopeIdentifier(project: {
    id: string;
    identifier?: string;
  }): string {
    return String(project.identifier ?? project.id);
  }

  async handleSave() {
    this.isLoading = true;
    this.errorMessage = '';
    let bitbucketDcDetails: BitbucketDcConnectionDetails | null = null;
    if (this.isBitbucketDc) {
      bitbucketDcDetails = this.prepareBitbucketDc();
      if (!bitbucketDcDetails) {
        this.isLoading = false;
        return;
      }
    }
    const scopeRules = [];
    for (const org of this.orgs) {
      if (this.selectedOrgs[org.id]) {
        scopeRules.push({
          rule_type: 'INCLUDE',
          scope_type: 'ORGANIZATION',
          identifier: String(org.id),
        });
        if (this.includeFutureProjects) {
          for (const proj of this.projects[org.id] || []) {
            const projectIdentifier = this.projectScopeIdentifier(proj);
            if (!this.selectedProjects[org.id]?.[proj.id]) {
              scopeRules.push({
                rule_type: 'EXCLUDE',
                scope_type: 'PROJECT',
                identifier: projectIdentifier,
              });
            }
          }
        } else {
          for (const [projectId, selected] of Object.entries(
            this.selectedProjects[org.id] || {}
          )) {
            if (!selected) {
              continue;
            }
            const project = (this.projects[org.id] || []).find(
              (candidate) => candidate.id === projectId
            );
            scopeRules.push({
              rule_type: 'INCLUDE',
              scope_type: 'PROJECT',
              identifier: project
                ? this.projectScopeIdentifier(project)
                : String(projectId),
            });
          }
        }
      }
    }

    const connectionDetails: Record<string, unknown> = bitbucketDcDetails
      ? bitbucketDcDetails
      : this.trackerType === 'bitbucket'
        ? this.bitbucketConnectionDetails()
        : {
            username: this.trackerUsername,
          };
    const trackerData: any = {
      name: this.trackerName,
      type: this.trackerType,
      url: this.trackerUrl,
      scope_rules: scopeRules,
      // Updates persist connection_details. config is the legacy key.
      // Send both while older servers still read config.
      config: connectionDetails,
      connection_details: connectionDetails,
    };

    // Add auth-specific fields
    if (this.authMethod === 'github_app') {
      trackerData.auth_type = 'github_app';
      // No API key needed for GitHub App auth. On edit the tracker keeps its
      // existing installation binding, so only new trackers send the id.
      if (!this.tracker && this.githubInstallationId) {
        trackerData.github_installation_id = this.githubInstallationId;
      }
    } else {
      trackerData.auth_type = 'api_token';
      trackerData.api_key = this.trackerToken;
    }
    if (this.trackerType === 'bitbucket') {
      trackerData.url = BITBUCKET_WEB_URL;
      trackerData.auth_type = this.bitbucketAuthType;
    }
    if (bitbucketDcDetails) {
      // The tracker URL is the canonical instance URL (origin plus context
      // path); it must match connection_details.instance_url on the backend.
      // Only manual PAT auth is supported; an untouched token stays
      // 'unchanged' so the stored secret is preserved on edit.
      trackerData.url = bitbucketDcDetails.instance_url;
      trackerData.auth_type = 'api_token';
      trackerData.api_key = this.trackerToken;
    }

    try {
      let response;
      if (this.tracker) {
        response = await this._api.updateTracker(this.tracker.id, trackerData);
      } else {
        response = await this._api.addTracker(trackerData);
      }

      // Check for warnings in response and display them before closing
      if (response?.warnings && response.warnings.length > 0) {
        this.warningMessages = response.warnings;
        this.isLoading = false;
        // Dispatch event but don't close modal yet - let user see the warnings
        this.dispatchEvent(
          new CustomEvent(this.tracker ? 'tracker-updated' : 'tracker-added', {
            detail: { tracker: response, hasWarnings: true },
          })
        );
        return;
      }

      // No warnings - dispatch event and close
      this.dispatchEvent(
        new CustomEvent(this.tracker ? 'tracker-updated' : 'tracker-added', {
          detail: { tracker: response },
        })
      );
      this.closeModal(true);
    } catch (error: any) {
      this.errorMessage = error.message;
    } finally {
      this.isLoading = false;
    }
  }

  closeModal(success = false) {
    if (typeof success !== 'boolean') {
      success = false;
    }
    const event = new CustomEvent('close-modal', {
      bubbles: true,
      composed: true,
      detail: { success },
    });
    this.dispatchEvent(event);
    this.opened = false;
  }
}
