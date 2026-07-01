import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '../../components/view-header.ts';
import '../../components/resource-actions.ts';
import {
  fetchWithAuth,
  getFeatures,
  deleteTracker,
  listOrganizations,
  listProjects,
  syncTracker,
  type FeaturesResponse,
} from '../../api';
import type { Organization, Project } from '../../types';
import {
  describeTrackerScope,
  groupProjectsByOrganization,
} from '../../utils/tracker-scope';
import consoleStyles from '../../styles/console-styles.css?inline';

interface TrackerDetail {
  id: string;
  name: string;
  tracker_type: string;
  created: string;
  last_updated: string;
  is_valid: boolean;
  validation_message?: string;
  url?: string;
  scope_rules?: Array<{
    scope_type: string;
    rule_type: string;
    identifier: string;
  }>;
}

@customElement('tracker-detail-view')
export class TrackerDetailView extends LitElement {
  @state()
  private _tracker: TrackerDetail | null = null;

  @state()
  private _projects: Project[] = [];

  @state()
  private _organizations: Organization[] = [];

  @state()
  private _loading = true;

  @state()
  private _error: string | null = null;

  @state()
  private _editingTracker: TrackerDetail | null = null;

  @state()
  private _features: FeaturesResponse['features'] = {};

  @state()
  private _featuresLoaded = false;

  @state()
  private _syncing = false;

  @state()
  private _syncMessage: string | null = null;

  private _trackerId = '';

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .tracker-header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-small);
      }

      .tracker-icon {
        font-size: 2.5rem;
        color: var(--sl-color-primary-600);
      }

      .tracker-meta {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .tracker-meta span {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }

      .scope-summary {
        margin: 0 0 var(--sl-spacing-large) 0;
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
        border-radius: var(--sl-border-radius-medium);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        line-height: 1.5;
      }

      .scope-summary strong {
        color: var(--sl-color-neutral-900);
      }

      .section-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        flex-wrap: wrap;
        margin: var(--sl-spacing-large) 0 var(--sl-spacing-medium) 0;
      }

      .section-title {
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
        margin: 0;
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
      }

      .analytics-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: var(--sl-spacing-medium);
      }

      .analytics-card {
        cursor: pointer;
        transition: box-shadow 0.2s ease;
      }

      .analytics-card:hover {
        box-shadow: var(--sl-shadow-medium);
      }

      .analytics-card .card-header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-small);
      }

      .analytics-card .card-header sl-icon {
        font-size: 1.25rem;
        color: var(--sl-color-primary-600);
      }

      .analytics-card .card-header h3 {
        margin: 0;
        font-size: var(--sl-font-size-medium);
      }

      .analytics-card p {
        margin: 0;
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .org-group {
        margin-bottom: var(--sl-spacing-large);
      }

      .org-group:last-child {
        margin-bottom: 0;
      }

      .org-header {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        margin-bottom: var(--sl-spacing-small);
        font-size: var(--sl-font-size-small);
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-700);
      }

      .projects-list {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
      }

      .project-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
        border-radius: var(--sl-border-radius-medium);
        font-size: var(--sl-font-size-small);
      }

      .project-info {
        display: flex;
        align-items: flex-start;
        gap: var(--sl-spacing-small);
        min-width: 0;
      }

      .project-text {
        min-width: 0;
      }

      .project-name {
        font-weight: var(--sl-font-weight-semibold);
        color: var(--sl-color-neutral-900);
      }

      .project-description {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-x-small);
        margin-top: 2px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .header-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: var(--sl-spacing-small);
        flex: 1;
        min-width: min(100%, 360px);
      }

      .no-analytics,
      .no-projects {
        padding: var(--sl-spacing-large);
        text-align: center;
        color: var(--sl-color-neutral-500);
        background: var(--sl-color-neutral-50);
        border-radius: var(--sl-border-radius-medium);
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this._trackerId = (this as any).location?.params?.trackerId || '';
    this._loadData();
  }

  private async _loadProjectsForTracker() {
    const [organizations, allProjects] = await Promise.all([
      listOrganizations().catch(() => []),
      listProjects().catch(() => []),
    ]);
    this._organizations = organizations.filter(
      (org) => org.tracker_id === this._trackerId
    );
    const orgIds = new Set(this._organizations.map((org) => org.id));
    this._projects = allProjects.filter((project) =>
      orgIds.has(project.organization_id)
    );
  }

  private async _loadData() {
    this._loading = true;
    this._error = null;

    try {
      const [trackerRes, featuresRes] = await Promise.all([
        fetchWithAuth(`/api/v1/trackers/${this._trackerId}`),
        getFeatures(),
      ]);

      if (!trackerRes.ok) {
        throw new Error('Tracker not found');
      }

      this._tracker = await trackerRes.json();
      this._features = featuresRes.features;
      this._featuresLoaded = true;
      await this._loadProjectsForTracker();
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Failed to load tracker';
    } finally {
      this._loading = false;
    }
  }

  private _describeScope(): string {
    return describeTrackerScope(
      this._tracker?.scope_rules,
      this._organizations,
      this._projects
    );
  }

  private _projectsByOrganization() {
    return groupProjectsByOrganization(this._organizations, this._projects);
  }

  private _hasAnyAnalyticsFeature(): boolean {
    return !!(
      this._features.issue_compliance ||
      this._features.issue_duplicates ||
      this._features.issue_dependencies
    );
  }

  private _getTrackerIcon(): string {
    const type = this._tracker?.tracker_type?.toLowerCase() || '';
    if (type.includes('jira')) return 'git';
    if (type.includes('github')) return 'github';
    if (type.includes('gitlab')) return 'gitlab';
    return 'box-seam';
  }

  private _shortProjectId(projectId: string): string {
    return projectId.split('-')[0];
  }

  private _getProjectIds(): string {
    return this._projects.map((p) => this._shortProjectId(p.id)).join(',');
  }

  private _buildIssuesUrl(subpath: string, projectIds?: string[]): string {
    const ids =
      projectIds && projectIds.length > 0
        ? projectIds.map((id) => this._shortProjectId(id)).join(',')
        : this._getProjectIds();
    const base = `/console/issues${subpath}`;
    return ids ? `${base}?projects=${ids}` : base;
  }

  private handleEdit() {
    if (this._tracker) {
      this._editingTracker = this._tracker;
    }
  }

  private async handleDelete() {
    if (!this._tracker) return;
    if (
      !confirm(
        `Are you sure you want to delete the tracker "${this._tracker.name}"?`
      )
    ) {
      return;
    }

    this._loading = true;
    try {
      await deleteTracker(this._trackerId);
      Router.go('/console/trackers');
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Failed to delete tracker';
      this._loading = false;
    }
  }

  private async handleSync() {
    if (!this._tracker || this._syncing) return;

    this._syncing = true;
    this._error = null;
    this._syncMessage = null;

    try {
      await syncTracker(this._trackerId);
      this._syncMessage =
        'Sync queued. Projects will refresh as the tracker finishes scanning.';
      window.setTimeout(() => void this._loadProjectsForTracker(), 3000);
      window.setTimeout(() => void this._loadProjectsForTracker(), 10000);
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Failed to sync tracker';
    } finally {
      this._syncing = false;
    }
  }

  private _handleTrackerUpdated() {
    this._editingTracker = null;
    this._loadData();
  }

  private _closeAddTrackerForm() {
    this._editingTracker = null;
  }

  private _renderProjectGroups() {
    const groups = this._projectsByOrganization();
    if (groups.length === 0) {
      return html`<div class="no-projects">
        No projects synced yet. Run <strong>Sync Now</strong> or edit the
        tracker to choose groups and projects, then sync again.
      </div>`;
    }

    return html`
      ${groups.map(
        (group) => html`
          <div class="org-group">
            <div class="org-header">
              <sl-icon name="collection"></sl-icon>
              ${group.organization.name}
              <sl-badge variant="neutral" pill
                >${group.projects.length}</sl-badge
              >
            </div>
            <div class="projects-list">
              ${group.projects.map(
                (project) => html`
                  <div class="project-row">
                    <div class="project-info">
                      <sl-icon
                        name="folder"
                        style="color: var(--sl-color-primary-500); flex-shrink: 0;"
                      ></sl-icon>
                      <div class="project-text">
                        <div class="project-name">${project.name}</div>
                        ${
                          project.description
                            ? html`<div class="project-description">
                                ${project.description}
                              </div>`
                            : ''
                        }
                      </div>
                    </div>
                    <sl-button
                      size="small"
                      variant="default"
                      href=${this._buildIssuesUrl('', [project.id])}
                    >
                      View issues
                      <sl-icon slot="suffix" name="arrow-right"></sl-icon>
                    </sl-button>
                  </div>
                `
              )}
            </div>
          </div>
        `
      )}
    `;
  }

  render() {
    if (this._loading) {
      return html`
        <div
          style="display: flex; justify-content: center; padding: var(--sl-spacing-2x-large);"
        >
          <sl-spinner style="font-size: 2rem;"></sl-spinner>
        </div>
      `;
    }

    if (this._error || !this._tracker) {
      return html`
        <view-header headerText="Tracker Details" width="narrow">
          <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
            <sl-button
              variant="text"
              size="small"
              href="/console/trackers"
              style="margin-left: -12px;"
            >
              <sl-icon slot="prefix" name="arrow-left"></sl-icon> Back to
              Trackers
            </sl-button>
          </div>
        </view-header>
        <div class="column-layout narrow" style="padding-top: 0;">
          <div class="main-column">
            <sl-alert variant="danger" open>
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              ${this._error || 'Tracker not found'}
            </sl-alert>
          </div>
        </div>
      `;
    }

    const tracker = this._tracker;
    const createdDate = new Date(tracker.created).toLocaleDateString();
    const updatedDate = new Date(tracker.last_updated).toLocaleDateString();
    const icon = this._getTrackerIcon();
    const hasAnalytics = this._hasAnyAnalyticsFeature();
    const hasProjects = this._projects.length > 0;

    return html`
      ${
        this._editingTracker
          ? html`<add-tracker-modal
              .tracker=${this._editingTracker}
              @tracker-updated=${this._handleTrackerUpdated}
              @close-modal=${this._closeAddTrackerForm}
            ></add-tracker-modal>`
          : ''
      }
      <view-header headerText=${tracker.name} width="narrow">
        <div slot="top" style="margin-bottom: var(--sl-spacing-small);">
          <sl-button
            variant="text"
            size="small"
            href="/console/trackers"
            style="margin-left: -12px;"
          >
            <sl-icon slot="prefix" name="arrow-left"></sl-icon> Back to Trackers
          </sl-button>
        </div>
        <div slot="main-column" class="header-actions">
          <resource-actions
            .collapseOverflow=${false}
            .actions=${[
              {
                id: 'edit',
                label: 'Edit',
                icon: 'pencil',
                onClick: () => this.handleEdit(),
              },
              {
                id: 'sync',
                label: 'Sync Now',
                icon: 'arrow-repeat',
                loading: this._syncing,
                disabled: this._syncing,
                onClick: () => this.handleSync(),
              },
              {
                id: 'delete',
                label: 'Delete',
                icon: 'trash',
                variant: 'danger',
                onClick: () => this.handleDelete(),
              },
            ]}
          ></resource-actions>
        </div>
      </view-header>
      <div class="column-layout narrow" style="padding-top: 0;">
        <div class="main-column">
          <div class="tracker-header">
            <sl-icon class="tracker-icon" name=${icon}></sl-icon>
            <sl-badge variant=${tracker.is_valid ? 'success' : 'warning'} pill>
              ${tracker.is_valid ? 'Connected' : 'Not validated'}
            </sl-badge>
          </div>

          <div class="tracker-meta">
            <span>
              <sl-icon name="tag"></sl-icon>
              ${tracker.tracker_type}
            </span>
            <span>
              <sl-icon name="calendar3"></sl-icon>
              Created ${createdDate}
            </span>
            <span>
              <sl-icon name="clock-history"></sl-icon>
              Updated ${updatedDate}
            </span>
            ${
              tracker.url
                ? html`<span>
                    <sl-icon name="link-45deg"></sl-icon>
                    ${tracker.url}
                  </span>`
                : ''
            }
          </div>

          <p class="scope-summary">
            <strong>Scope:</strong> ${this._describeScope()}
            ${
              tracker.scope_rules && tracker.scope_rules.length > 0
                ? html` Use <strong>Edit</strong> to change which groups and
                    projects are scanned.`
                : ''
            }
          </p>

          ${
            this._syncMessage
              ? html`<sl-alert
                  variant="success"
                  open
                  style="margin-bottom: 1rem;"
                >
                  <sl-icon slot="icon" name="check-circle"></sl-icon>
                  ${this._syncMessage}
                </sl-alert>`
              : ''
          }
          ${
            this._error
              ? html`<sl-alert
                  variant="danger"
                  open
                  style="margin-bottom: 1rem;"
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this._error}
                </sl-alert>`
              : ''
          }

          <div class="section-header">
            <h2 class="section-title">
              Synced projects
              ${
                hasProjects
                  ? html`<sl-badge variant="neutral" pill
                      >${this._projects.length}</sl-badge
                    >`
                  : ''
              }
            </h2>
            ${
              hasProjects
                ? html`<sl-button
                    variant="primary"
                    size="small"
                    href=${this._buildIssuesUrl('')}
                  >
                    View all issues
                    <sl-icon slot="suffix" name="arrow-right"></sl-icon>
                  </sl-button>`
                : ''
            }
          </div>

          ${this._renderProjectGroups()}

          <sl-divider></sl-divider>

          <h2 class="section-title">Issue analytics</h2>

          ${
            hasAnalytics && hasProjects
              ? html`
                  <div class="analytics-grid">
                    ${
                      this._features.issue_duplicates
                        ? html`
                            <a
                              href=${this._buildIssuesUrl('')}
                              style="text-decoration: none; color: inherit;"
                            >
                              <sl-card class="analytics-card">
                                <div class="card-header">
                                  <sl-icon name="intersect"></sl-icon>
                                  <h3>Similarity</h3>
                                </div>
                                <p>
                                  Find duplicate and overlapping issues across
                                  ${this._projects.length} synced
                                  project${this._projects.length === 1 ? '' : 's'}.
                                </p>
                              </sl-card>
                            </a>
                          `
                        : ''
                    }
                    ${
                      this._features.issue_compliance
                        ? html`
                            <a
                              href=${this._buildIssuesUrl('/compliance')}
                              style="text-decoration: none; color: inherit;"
                            >
                              <sl-card class="analytics-card">
                                <div class="card-header">
                                  <sl-icon name="clipboard-check"></sl-icon>
                                  <h3>Compliance</h3>
                                </div>
                                <p>
                                  Evaluate issue quality against compliance
                                  metrics for synced projects.
                                </p>
                              </sl-card>
                            </a>
                          `
                        : ''
                    }
                    ${
                      this._features.issue_dependencies
                        ? html`
                            <a
                              href=${this._buildIssuesUrl('/dependencies')}
                              style="text-decoration: none; color: inherit;"
                            >
                              <sl-card class="analytics-card">
                                <div class="card-header">
                                  <sl-icon name="diagram-3"></sl-icon>
                                  <h3>Dependencies</h3>
                                </div>
                                <p>
                                  Detect unmapped dependencies between issues in
                                  synced projects.
                                </p>
                              </sl-card>
                            </a>
                          `
                        : ''
                    }
                  </div>
                `
              : html`
                  <div class="no-analytics">
                    ${
                      !hasProjects
                        ? 'Issue analytics become available after projects sync. Run Sync Now once scope is configured.'
                        : 'Issue analytics features are not enabled for this instance.'
                    }
                  </div>
                `
          }
        </div>
      </div>
    `;
  }
}
