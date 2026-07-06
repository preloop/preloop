import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state, property } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { nothing } from 'lit';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '../../components/project-filter-modal.ts';
import '../../components/single-issue-detail-view.ts';
import '../../components/improve-compliance-modal.ts';
import '../../components/pagination-controls.ts';
import { getStatusVariant, getComplianceVariant } from '../../utils/verdict';
import {
  listProjects,
  listOrganizations,
  searchIssues,
  getIssueCompliance,
  getCompliancePrompts,
} from '../../api';
import type {
  Project,
  Organization,
  Issue,
  IssueComplianceResult,
  CompliancePromptMetadata,
} from '../../types';

import consoleStyles from '../../styles/console-styles.css?inline';

@customElement('issues-compliance-view')
export class IssuesComplianceView extends LitElement {
  private readonly INFO_ALERT_DISMISSED_KEY =
    'preloop-issues-compliance-info-alert-dismissed';

  @state()
  private _isInfoAlertOpen = false;

  @state()
  private _searchQuery = '';

  @state()
  private _hasSearched = false;

  @state()
  private _issues: Issue[] = [];

  @state()
  private _complianceResults: Record<string, IssueComplianceResult> = {};

  @state()
  private _loadingCompliance: Record<string, boolean> = {};

  @state()
  private _loading = false;

  @state()
  private _error: string | null = null;

  @state()
  private _expandedRowKey: string | null = null;

  @state()
  private _isFilterModalOpen = false;

  @state()
  private _isImproveComplianceModalOpen = false;

  @state()
  private _selectedIssueForCompliance: Issue | null = null;

  @state()
  private _selectedProjectIds: string[] = [];

  @state()
  private _selectedStatus: 'opened' | 'closed' | 'all' = 'opened';

  @state()
  private _selectedCompliancePrompt = 'dor_compliance_v1';

  @state()
  private _compliancePrompts: CompliancePromptMetadata[] = [];

  @state()
  private _allProjects: Project[] = [];

  @state()
  private _hasProjects = true;

  @state()
  private _organizations: Organization[] = [];

  @state()
  private _initialLoadComplete = false;

  @state()
  private _updateSummary: string | null = null;

  @state()
  private _currentPage = 1;

  @state()
  private _hasMorePages = false;

  private _pageSize = 10;

  private _isViewActive = false;

  private get _complianceMetricName() {
    const firstResult = Object.values(this._complianceResults)[0];
    return firstResult ? firstResult.short_name : 'Compliance';
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .table-card {
        width: 100%;
        --padding: 0;
        border-spacing: 0;
      }

      .styled-table .issue-id {
        font-weight: var(--sl-font-weight-semibold);
      }

      .issue-key {
        color: var(--sl-color-neutral-600);
      }

      .faint-row {
        opacity: 0.5;
        transition: opacity 0.3s ease-in-out;
      }

      .clickable-row {
        cursor: pointer;
      }
      .row-expanded {
        background-color: var(--sl-color-primary-50);
      }

      .loading-overlay {
        color: white;
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        gap: var(--sl-spacing-medium);
        z-index: 10000;
      }
      .chart-header {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }

      sl-icon {
        font-size: 1rem;
      }

      .placeholder-content {
        text-align: center;
      }

      .search-bar {
        display: flex;
        gap: var(--sl-spacing-small);
        align-items: center;
        margin-bottom: var(--sl-spacing-medium);
      }

      .search-bar sl-input {
        flex-grow: 1;
      }

      .review-section {
        margin-top: 3rem;
      }

      .issue-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        background-color: var(--sl-color-neutral-100);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        white-space: pre-line;
        overflow-wrap: break-word;
        max-height: 400px;
        overflow-y: auto;
      }

      .compliance-reason,
      .compliance-suggestion {
        margin-top: var(--sl-spacing-small);
      }

      .compliance-title {
        display: block;
        margin-top: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-x-small);
        font-weight: var(--sl-font-weight-semibold);
      }

      .review-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--sl-spacing-medium);
      }

      .score-container {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: var(--sl-spacing-medium);
      }

      .score-container .compliance-title {
        margin: 0;
      }

      .improve-button-container {
        display: flex;
        justify-content: flex-end;
        margin-top: var(--sl-spacing-medium);
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    this._isViewActive = true;
    const isDismissed = localStorage.getItem(this.INFO_ALERT_DISMISSED_KEY);
    this._isInfoAlertOpen = isDismissed !== 'true';
    // Fetch dynamic data first
    await this.fetchCompliancePrompts();
    await this.fetchProjects();

    this.parseUrlAndUpdateState();
    // Fetch issues on initial load, regardless of search query
    this.fetchIssues();
    this.fetchOrganizations();
    this._initialLoadComplete = true;
    window.addEventListener('popstate', this.handlePopState);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._isViewActive = false;
    window.removeEventListener('popstate', this.handlePopState);

    // Check if we are still on the issues path before cleaning up.
    // This prevents a race condition where the URL of the *next* page is cleaned.
    if (window.location.pathname.includes('/issues')) {
      window.history.replaceState({}, '', window.location.pathname);
    }
  }

  private handlePopState = () => {
    this.parseUrlAndUpdateState();
    this.fetchIssues();
  };

  private parseUrlAndUpdateState() {
    const params = new URLSearchParams(window.location.search);
    this._searchQuery = params.get('query') || '';
    const prompt = params.get('prompt');
    if (prompt && this._compliancePrompts.some((p) => p.id === prompt)) {
      this._selectedCompliancePrompt = prompt;
    }
    const shortProjectIds = params.get('projects');
    if (shortProjectIds && this._allProjects.length > 0) {
      const shortIdSet = new Set(shortProjectIds.split(','));
      this._selectedProjectIds = this._allProjects
        .filter((p) => shortIdSet.has(p.id.split('-')[0]))
        .map((p) => p.id);
    } else {
      this._selectedProjectIds = [];
    }
    this._expandedRowKey = params.get('selectedIssue') || null;
    this._currentPage = Number(params.get('page')) || 1;
    const status = params.get('status');
    if (status && ['opened', 'closed', 'all'].includes(status)) {
      this._selectedStatus = status;
    }
  }

  private _updateUrl() {
    // Only update the URL if we are on the issues page.
    if (!window.location.pathname.includes('/issues')) {
      return;
    }

    const params = new URLSearchParams();
    params.set('status', this._selectedStatus);
    params.set('prompt', this._selectedCompliancePrompt);
    if (this._searchQuery) {
      params.set('query', this._searchQuery);
    }
    if (this._selectedProjectIds.length > 0) {
      const shortProjectIds = this._selectedProjectIds.map(
        (id) => id.split('-')[0]
      );
      params.set('projects', shortProjectIds.join(','));
    }
    if (this._expandedRowKey) {
      params.set('selectedIssue', this._expandedRowKey);
    } else {
      params.delete('selectedIssue');
    }
    params.set('page', this._currentPage.toString());

    const newUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.pushState({}, '', newUrl);
  }

  async fetchInitialData() {
    this.fetchIssues();
    this.fetchProjects();
    this.fetchOrganizations();
  }

  async fetchProjects() {
    try {
      this._allProjects = await listProjects();
      this._hasProjects = this._allProjects.length > 0;
    } catch (error) {
      console.error('Failed to fetch project list:', error);
      this._hasProjects = false; // Set to false on error
    }
  }

  async fetchOrganizations() {
    try {
      this._organizations = await listOrganizations();
    } catch (error) {
      console.error('Failed to fetch organization list:', error);
    }
  }

  async fetchCompliancePrompts() {
    try {
      this._compliancePrompts = await getCompliancePrompts();
      // Ensure a valid prompt is always selected
      if (
        !this._compliancePrompts.some(
          (p) => p.id === this._selectedCompliancePrompt
        )
      ) {
        this._selectedCompliancePrompt = this._compliancePrompts[0]?.id || '';
      }
    } catch (error) {
      console.error('Failed to fetch compliance prompts:', error);
    }
  }

  async fetchIssues() {
    this._loading = true;
    this._error = null;
    this._hasSearched = true;

    try {
      const project_ids =
        this._selectedProjectIds.length > 0
          ? this._selectedProjectIds
          : undefined;
      const skip = (this._currentPage - 1) * this._pageSize;
      console.log(`Searching with query: "${this._searchQuery}"`);

      // Only include query parameter if it's not empty
      const searchParams: any = {
        project_ids: project_ids,
        status: this._selectedStatus,
        limit: this._pageSize,
        skip: skip,
      };

      if (this._searchQuery && this._searchQuery.trim()) {
        searchParams.query = this._searchQuery;
      }

      const issues = await searchIssues(searchParams);
      this._issues = issues;
      this._hasMorePages = issues.length === this._pageSize;
      this._updateUrl(); // Update URL after fetching
      this.fetchComplianceResults();
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'An unknown error occurred.';
      console.error('Failed to fetch issues:', error);
    } finally {
      this._loading = false;
    }
  }

  async fetchComplianceResults() {
    for (const issue of this._issues) {
      if (!this._isViewActive) {
        break;
      }

      const issueId = issue.id;
      // Skip if we already have the result or if it's already loading.
      if (
        this._complianceResults[issueId] ||
        this._loadingCompliance[issueId]
      ) {
        continue;
      }

      this._loadingCompliance = { ...this._loadingCompliance, [issueId]: true };

      try {
        const result = await getIssueCompliance(
          issueId,
          this._selectedCompliancePrompt
        );
        this._complianceResults = {
          ...this._complianceResults,
          [issueId]: result,
        };
      } catch (error) {
        console.error(
          `Failed to fetch compliance result for issue ${issueId}:`,
          error
        );
      } finally {
        this._loadingCompliance = {
          ...this._loadingCompliance,
          [issueId]: false,
        };
      }
    }
  }

  private _toggleRow(issueId: string) {
    if (this._expandedRowKey === issueId) {
      this._expandedRowKey = null;
    } else {
      this._expandedRowKey = issueId;
    }
    this._updateUrl();
  }

  private _openImproveComplianceModal(issue: Issue) {
    this._selectedIssueForCompliance = issue;
    this._isImproveComplianceModalOpen = true;
  }

  private _handleComplianceModalClose() {
    this._isImproveComplianceModalOpen = false;
    this._selectedIssueForCompliance = null;
  }

  private _handleComplianceUpdate(event: CustomEvent) {
    const { issueId, summary } = event.detail;

    if (summary) {
      this._updateSummary = summary;
    }

    // Delete the stale local compliance data to allow it to be refetched.
    const newComplianceResults = { ...this._complianceResults };
    if (issueId in newComplianceResults) {
      delete newComplianceResults[issueId];
      this._complianceResults = newComplianceResults;
    }

    // Refetch the issues list. `fetchComplianceResults` will be called automatically.
    this._currentPage = 1;
    this.fetchIssues();
  }

  private _handleMenuAction(e: CustomEvent, issue: Issue) {
    const action = e.detail.item.value;
    if (action === 'improve-compliance') {
      this._openImproveComplianceModal(issue);
    }
  }

  private _openFilterModal() {
    this._isFilterModalOpen = true;
  }

  private _removeProjectFilter(projectIdToRemove: string) {
    this._selectedProjectIds = this._selectedProjectIds.filter(
      (id) => id !== projectIdToRemove
    );
    this.fetchIssues();
  }

  private _clearAllFilters() {
    this._selectedProjectIds = [];
    this.fetchIssues();
  }

  private _renderActiveFilters() {
    if (
      this._selectedProjectIds.length === 0 &&
      this._selectedStatus === 'opened'
    ) {
      return html``;
    }

    const selectedProjects = this._selectedProjectIds
      .map((id) => this._allProjects.find((p) => p.id.toString() === id))
      .filter(Boolean) as Project[];

    return html`
      <div class="active-filters">
        <span>Filtered by:</span>
        ${selectedProjects.map(
          (project) => html`
            <sl-tag
              size="medium"
              removable
              @sl-remove=${() =>
                this._removeProjectFilter(project.id.toString())}
            >
              ${project.name}
            </sl-tag>
          `
        )}
        ${
          this._selectedStatus !== 'opened'
            ? html`
                <sl-tag
                  size="medium"
                  removable
                  @sl-remove=${() => this._clearStatusFilter()}
                >
                  ${this._selectedStatus === 'closed' ? 'Closed' : 'All'}
                </sl-tag>
              `
            : ''
        }
        <sl-button size="small" pill @click=${this._clearAllFilters}
          >Clear all</sl-button
        >
      </div>
    `;
  }

  private _clearStatusFilter() {
    this._selectedStatus = 'opened';
    this.fetchIssues();
  }

  private handleInfoAlertHide() {
    localStorage.setItem(this.INFO_ALERT_DISMISSED_KEY, 'true');
    this._isInfoAlertOpen = false;
  }

  private handleSearchInput(event: Event) {
    const input = event.target as HTMLInputElement;
    this._searchQuery = input.value;
  }

  private handleSearch(event: Event) {
    event.preventDefault();
    this._currentPage = 1;
    this.fetchIssues();
  }

  private _handleComplianceTypeSelect(e: CustomEvent) {
    const selectedValue = e.detail.item.value;
    if (this._compliancePrompts.some((p) => p.id === selectedValue)) {
      this._selectedCompliancePrompt = selectedValue;
      // When the type changes, we need to refetch the compliance data.
      this._complianceResults = {}; // Clear old results
      this.fetchComplianceResults();
      this._updateUrl(); // Also update the URL
    }
  }

  private _renderSearchBar() {
    const selectedPrompt = this._compliancePrompts.find(
      (p) => p.id === this._selectedCompliancePrompt
    );

    return html`
      <div class="search-bar">
        <sl-input
          placeholder="Search issues by title, description, or ID..."
          .value=${this._searchQuery}
          @sl-input=${(e: Event) =>
            (this._searchQuery = (e.target as HTMLInputElement).value)}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === 'Enter') this.handleSearch(e);
          }}
          clearable
        >
          <sl-icon name="search" slot="prefix"></sl-icon>
        </sl-input>
        <sl-dropdown>
          <sl-button slot="trigger" caret>
            ${selectedPrompt ? selectedPrompt.name : 'Select Type'}
          </sl-button>
          <sl-menu @sl-select=${this._handleComplianceTypeSelect}>
            ${this._compliancePrompts.map(
              (prompt) =>
                html`<sl-menu-item value=${prompt.id}
                  >${prompt.name}</sl-menu-item
                >`
            )}
          </sl-menu>
        </sl-dropdown>
        <sl-button-group>
          <sl-tooltip content="Filter by project">
            <sl-button @click=${() => (this._isFilterModalOpen = true)}>
              <sl-icon name="filter"></sl-icon>
            </sl-button>
          </sl-tooltip>
          <sl-button @click=${this.handleSearch} variant="primary">
            Search
          </sl-button>
        </sl-button-group>
      </div>
    `;
  }

  private renderIssueTable() {
    if (this._loading && this._issues.length === 0) {
      return html`<div class="loading-container">
        <sl-spinner></sl-spinner>
        <span>Loading issues...</span>
      </div>`;
    }

    if (this._error) {
      return html`<div class="error">${this._error}</div>`;
    }

    if (this._issues.length === 0) {
      return html`<div class="placeholder-content">
        <h3>No issues found</h3>
        <p>Your search did not return any issues.</p>
      </div>`;
    }

    return html`
      <div class="table-container">
        <sl-card class="table-card">
          <table class="styled-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Title</th>
                <th>Project</th>
                <th>Status</th>
                <th>${this._complianceMetricName}</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              ${this._issues.map((issue) => {
                const issueId = issue.id;
                const isExpanded = this._expandedRowKey === issueId;
                const project = this._allProjects.find(
                  (p) => p.id === issue.project_id
                );
                const complianceResult = this._complianceResults[issue.id];

                return html`
                  <tr
                    class="clickable-row ${isExpanded ? 'row-expanded' : ''}"
                    @click=${() => this._toggleRow(issueId)}
                  >
                    <td>
                      <a
                        href="${issue.url}"
                        target="_blank"
                        @click=${(e: Event) => e.stopPropagation()}
                        >${issue.key}</a
                      >
                    </td>
                    <td>${issue.title}</td>
                    <td>${project?.name || 'N/A'}</td>
                    <td>
                      <sl-badge variant=${getStatusVariant(issue.status)}>
                        ${issue.status}
                      </sl-badge>
                    </td>
                    <td>
                      ${when(
                        this._loadingCompliance[issue.id],
                        () => html`<sl-spinner></sl-spinner>`,
                        () => {
                          return complianceResult
                            ? html`
                                <sl-tooltip content=${complianceResult.reason}>
                                  <sl-badge
                                    variant=${getComplianceVariant(
                                      complianceResult.compliance_factor
                                    )}
                                    pill
                                  >
                                    ${(
                                      complianceResult.compliance_factor * 100
                                    ).toFixed(0)}%
                                  </sl-badge>
                                </sl-tooltip>
                              `
                            : html`<span>-</span>`;
                        }
                      )}
                    </td>
                    <td>
                      <sl-button-group>
                        <sl-button
                          size="small"
                          @click=${(e: Event) => {
                            e.stopPropagation();
                            this._openImproveComplianceModal(issue);
                          }}
                          ?disabled=${
                            !complianceResult ||
                            complianceResult.compliance_factor === 0 ||
                            complianceResult.compliance_factor === 1
                          }
                        >
                          Improve
                        </sl-button>
                        <sl-dropdown
                          @click=${(e: Event) => e.stopPropagation()}
                          ?disabled=${
                            !complianceResult ||
                            complianceResult.compliance_factor === 0 ||
                            complianceResult.compliance_factor === 1
                          }
                        >
                          <sl-button
                            slot="trigger"
                            size="small"
                            title="Actions"
                          >
                            <sl-icon name="three-dots-vertical"></sl-icon>
                          </sl-button>
                          <sl-menu
                            @sl-select=${(e: CustomEvent) =>
                              this._handleMenuAction(e, issue)}
                          >
                            <sl-menu-item value="improve-compliance">
                              <sl-icon
                                name="graph-up-arrow"
                                slot="prefix"
                              ></sl-icon>
                              Improve Compliance
                            </sl-menu-item>
                          </sl-menu>
                        </sl-dropdown>
                      </sl-button-group>
                    </td>
                  </tr>
                  ${
                    isExpanded
                      ? html`
                          <tr class="inline-detail-row">
                            <td colspan="6">
                              <div class="detail-view-card">
                                <single-issue-detail-view .issue=${issue}>
                                  <div slot="additional-info">
                                    ${this._renderComplianceDetails(
                                      issue,
                                      complianceResult
                                    )}
                                  </div>
                                </single-issue-detail-view>
                              </div>
                            </td>
                          </tr>
                        `
                      : ''
                  }
                `;
              })}
            </tbody>
          </table>
        </sl-card>
        <pagination-controls
          .currentPage=${this._currentPage}
          .hasMorePages=${this._hasMorePages}
          .loading=${this._loading}
          @prev-page=${this._goToPreviousPage}
          @next-page=${this._goToNextPage}
        ></pagination-controls>
      </div>
    `;
  }

  private _goToNextPage() {
    if (this._hasMorePages) {
      this._currentPage += 1;
      this.fetchIssues();
    }
  }

  private _goToPreviousPage() {
    if (this._currentPage > 1) {
      this._currentPage -= 1;
      this.fetchIssues();
    }
  }

  private _renderNoProjectsState() {
    return html`
      <div class="placeholder-content">
        <h3>No projects found</h3>
        <p>
          You don't have any projects. Please create a project to start tracking
          issues.
        </p>
      </div>
    `;
  }

  render() {
    return html`
      <view-header headerText="Issue Compliance" width="wide">
        <div slot="main-column">${this._renderSearchBar()}</div>
      </view-header>

      <project-filter-modal
        .isOpen=${this._isFilterModalOpen}
        .projects=${this._allProjects}
        .selectedProjectIds=${this._selectedProjectIds}
        .selectedStatus=${this._selectedStatus}
        .organizations=${this._organizations}
        .showResolution=${false}
        @on-close=${() => (this._isFilterModalOpen = false)}
        @on-apply=${this._applyFilters}
      ></project-filter-modal>

      <div class="column-layout wide">
        <div class="main-column">
          <div class="container">
            <sl-alert
              variant="primary"
              ?open=${this._isInfoAlertOpen}
              closable
              @sl-hide=${this.handleInfoAlertHide}
            >
              <sl-icon slot="icon" name="info-circle"></sl-icon>
              <strong
                >Check issues for compliance with guidelines and
                templates</strong
              ><br />
              Identify issues that do not comply with your organization's
              guidelines and templates. Review each issue, check the compliance
              score, and use the AI review to resolve or dismiss the suggestion.
            </sl-alert>

            ${this._renderActiveFilters()}
            ${when(
              this._updateSummary,
              () => html`
                <sl-alert
                  variant="success"
                  open
                  closable
                  @sl-after-hide=${() => (this._updateSummary = null)}
                >
                  <sl-icon slot="icon" name="check-circle"></sl-icon>
                  ${this._updateSummary}
                </sl-alert>
              `
            )}
            ${when(
              this._loading,
              () =>
                html`<div class="loading-overlay">
                  <sl-spinner></sl-spinner>
                  <span>Loading issues...</span>
                </div>`
            )}
            ${when(
              this._error,
              () => html`<div class="error">${this._error}</div>`
            )}
            ${when(!this._loading && !this._error && this._hasSearched, () =>
              this.renderIssueTable()
            )}
          </div>
        </div>
        <div class="side-column">
          <sl-card class="detail-view-card">
            <div slot="header">Issue Details</div>
            ${when(
              this._expandedRowKey,
              () => {
                const issue = this._issues.find(
                  (i) => i.id === this._expandedRowKey
                );
                if (issue) {
                  const complianceResult = this._complianceResults[issue.id];
                  return html`<single-issue-detail-view .issue=${issue}>
                    <div slot="additional-info">
                      ${this._renderComplianceDetails(issue, complianceResult)}
                    </div>
                  </single-issue-detail-view>`;
                } else {
                  return html`<div class="placeholder-content">
                    <sl-icon name="info-circle"></sl-icon>
                    <p>Select an issue to see details.</p>
                  </div>`;
                }
              },
              () =>
                html`<div class="placeholder-content">
                  <sl-icon name="info-circle"></sl-icon>
                  <p>Select an issue to see details.</p>
                </div>`
            )}
          </sl-card>
        </div>
      </div>

      <improve-compliance-modal
        .open=${this._isImproveComplianceModalOpen}
        .issue=${this._selectedIssueForCompliance}
        .promptName=${this._selectedCompliancePrompt}
        @close=${this._handleComplianceModalClose}
        @on-submit=${this._handleComplianceUpdate}
      >
      </improve-compliance-modal>
    `;
  }

  private _renderComplianceDetails(
    issue: Issue,
    complianceResult: IssueComplianceResult | null
  ) {
    if (!complianceResult) {
      return nothing;
    }

    return html`
      <div class="review-section">
        <div class="review-header">
          <h3>${complianceResult.name} Review</h3>
        </div>

        ${when(
          complianceResult.compliance_factor === 0,
          () => html`
            <sl-alert variant="warning" open>
              <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
              This issue could not be evaluated and requires manual review. It
              may be missing key information or be too vague.
            </sl-alert>
          `,
          () => html`
            <div class="score-container">
              <b class="compliance-title">Compliance Score</b>
              <sl-badge
                variant=${getComplianceVariant(
                  complianceResult.compliance_factor
                )}
                pill
              >
                ${(complianceResult.compliance_factor * 100).toFixed(0)}%
              </sl-badge>
            </div>
            <div>
              <b class="compliance-title">Reason</b>
              <div class="issue-description">${complianceResult.reason}</div>
            </div>
            <div>
              <b class="compliance-title">Suggestion for Improvement</b>
              <div class="issue-description">
                ${complianceResult.suggestion}
              </div>
            </div>
            <div class="improve-button-container">
              <sl-button
                size="small"
                @click=${() => this._openImproveComplianceModal(issue)}
                ?disabled=${
                  !complianceResult ||
                  complianceResult.compliance_factor === 0 ||
                  complianceResult.compliance_factor === 1
                }
              >
                Improve
              </sl-button>
            </div>
          `
        )}
      </div>
    `;
  }

  private _applyFilters(e: CustomEvent) {
    this._selectedProjectIds = e.detail.projectIds;
    this._selectedStatus = e.detail.status;
    this._isFilterModalOpen = false;
    this._currentPage = 1;
    this.fetchIssues();
  }

  private _handleProjectFilterChange(e: CustomEvent) {
    this._selectedProjectIds = e.detail.selectedProjectIds;
    this._currentPage = 1;
    this.fetchIssues();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'issues-compliance-view': IssuesComplianceView;
  }
}
