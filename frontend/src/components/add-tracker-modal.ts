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

@customElement('add-tracker-modal')
export class AddTrackerModal extends LitElement {
  @property({ type: Object })
  tracker: any = null;

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
      this.authMethod = this.tracker.auth_type || 'api_token';
      // For GitHub App auth, initialize installation ID from tracker
      if (
        this.tracker.auth_type === 'github_app' &&
        this.tracker.oauth_installation_id
      ) {
        this.githubInstallationId = String(this.tracker.oauth_installation_id);
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
        @sl-change=${(e: any) => {
          this.trackerType = e.target.value;
          const urlInput = this.shadowRoot?.querySelector(
            'sl-input[name="url"]'
          ) as HTMLInputElement;
          if (this.trackerType === 'gitlab') {
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
      </sl-select>
      <sl-input
        label="URL"
        name="url"
        .value=${this.trackerUrl}
        @sl-input=${(e: any) => (this.trackerUrl = e.target.value)}
        placeholder="e.g., https://github.example.com"
      ></sl-input>
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
        this.authMethod === 'github_app' && this.githubInstallationId
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
            : html`
                <sl-input
                  type="password"
                  label="API Key"
                  name="api_key"
                  .value=${this.trackerToken}
                  @sl-input=${(e: any) => (this.trackerToken = e.target.value)}
                  required
                ></sl-input>
              `
      }
    `;
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
                !isGitHubApp
                  ? this.projects[org.id]?.map(
                      (proj: any) => html`
                        <sl-tree-item
                          value="${proj.id}"
                          ?selected=${this.selectedProjects[org.id]?.[proj.id]}
                        >
                          ${proj.name}
                        </sl-tree-item>
                      `
                    )
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

    try {
      // For GitHub App auth, complete the installation and save directly
      // Users already select org/repo access during GitHub App installation
      if (this.authMethod === 'github_app' && this.githubInstallationId) {
        // Complete the installation to associate it with the account
        await this._api.completeGitHubInstallation({
          installation_id: this.githubInstallationId,
        });

        // For GitHub App, fetch installations to build scope rules automatically
        const installations = await this._api.getGitHubInstallations();
        // Use target_id (GitHub org/user ID) as the identifier for scope rules
        this.orgs = installations.map((inst) => ({
          id: String(inst.target_id),
          name: inst.target_login,
          type: inst.target_type,
        }));

        // Auto-select all orgs from the installation (user already selected during GitHub App setup)
        this.selectedOrgs = {};
        for (const org of this.orgs) {
          this.selectedOrgs[org.id] = true;
        }

        // Skip step 2 and save directly for new GitHub App trackers
        if (!this.tracker) {
          await this.handleSave();
          return;
        }

        // For editing existing trackers, show step 2
        this.step = 2;
      } else {
        // Standard API token flow
        const response = await this._api.validateTrackerToken(
          this.trackerType,
          this.trackerToken,
          this.trackerUrl,
          this.trackerUsername,
          this.tracker?.id
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
        this.tracker?.id
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
      const project = item.getAttribute('value');
      const org = item.parentElement?.getAttribute('value');
      if (!org || !project) return;
      newSelectedOrgs[org] = true;
      newSelectedProjects[org][project] = true;
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

    const trackerData: any = {
      name: this.trackerName,
      type: this.trackerType,
      url: this.trackerUrl,
      scope_rules: scopeRules,
      config: {
        username: this.trackerUsername,
      },
    };

    // Add auth-specific fields
    if (this.authMethod === 'github_app' && this.githubInstallationId) {
      trackerData.auth_type = 'github_app';
      trackerData.github_installation_id = this.githubInstallationId;
      // No API key needed for GitHub App auth
    } else {
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
