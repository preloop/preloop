import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';

import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

import '../../components/project-filter-modal.ts';
import '../../components/single-issue-detail-view.ts';
import {
  listProjects,
  searchIssues,
  detectIssueDependencies,
  extendIssueDependencyScan,
  commitIssueDependencies, // Import the new function
} from '../../api';
import type {
  Project,
  Issue,
  DependencyPair, // Import the type
  IssueStatus,
} from '../../types';
import consoleStyles from '../../styles/console-styles.css?inline';
import '../../components/pagination-controls.ts';
import { getStatusVariant } from '../../utils/verdict';

@customElement('issues-dependencies-view')
export class IssuesDependenciesView extends LitElement {
  @state()
  private _allProjects: Project[] = [];

  @state()
  private _selectedProjectId: string | null = null;

  @state()
  private _issues: Issue[] = [];

  @state()
  private _dependencies: DependencyPair[] = [];

  @state()
  private _dependencyMap: Map<
    string,
    { blocks: DependencyPair[]; blockedBy: DependencyPair[] }
  > = new Map();

  @state()
  private _loadingIssues = false;

  @state()
  private _loadingDependencies = false;

  @state()
  private _error: string | null = null;

  @state()
  private _isFilterModalOpen = false;

  @state()
  private _selectedStatus: IssueStatus | null = 'opened';

  @state()
  private _currentPage = 1;

  @state()
  private _hasMorePages = false;

  @state()
  private _expandedRowKey: string | null = null;

  @state()
  private _expandingIssueId: string | null = null;

  @state()
  private _committingDependencies: Set<string> = new Set();

  @state()
  private _committingAllForIssue: string | null = null;

  private _pageSize = 10;

  async connectedCallback() {
    super.connectedCallback();
  }

  async firstUpdated() {
    await this._fetchProjects();
    this.parseUrlAndUpdateState();
    if (this._allProjects.length > 0 && !this._selectedProjectId) {
      this._selectedProjectId = this._allProjects[0].id;
    }
    this.fetchIssues();
    this._updateUrl();
  }

  private async _fetchProjects() {
    try {
      this._allProjects = await listProjects();
    } catch (error) {
      this._error = 'Failed to load projects.';
      console.error(error);
    }
  }

  async fetchIssues() {
    if (!this._selectedProjectId) return;

    this._loadingIssues = true;
    this._error = null;
    this._dependencies = []; // Clear old dependencies
    this._dependencyMap.clear();

    try {
      const skip = (this._currentPage - 1) * this._pageSize;
      const issues = await searchIssues({
        project_ids: [this._selectedProjectId],
        limit: this._pageSize,
        skip: skip,
        status: this._selectedStatus || undefined,
      });
      this._issues = issues;
      this._hasMorePages = issues.length === this._pageSize;

      if (issues.length > 0) {
        this.fetchDependencies(issues.map((i) => i.id));
      }
    } catch (error) {
      this._error = 'Failed to load issues.';
      console.error(error);
    } finally {
      this._loadingIssues = false;
    }
  }

  async fetchDependencies(issueIds: string[]) {
    this._loadingDependencies = true;
    try {
      const result = await detectIssueDependencies(issueIds);
      this._dependencies = result.dependencies;
      this._processDependencies();
    } catch (error) {
      console.error('Failed to detect dependencies', error);
      // Silently fail for now, or show a non-blocking error
    } finally {
      this._loadingDependencies = false;
    }
  }

  private async _expandScanForRow(issueId: string) {
    this._expandingIssueId = issueId;
    this._loadingDependencies = true;
    this._error = null;
    try {
      // Extend the scan starting from the selected issue
      const result = await extendIssueDependencyScan([issueId], 10); // Extend by 10 issues

      // Merge new dependencies with existing ones
      const newDependencyPairs = [...this._dependencies];
      const existingPairs = new Set(
        newDependencyPairs.map(
          (p) => `${p.source_issue_id}-${p.dependent_issue_id}`
        )
      );

      for (const dep of result.dependencies) {
        const pairKey = `${dep.source_issue_id}-${dep.dependent_issue_id}`;
        if (!existingPairs.has(pairKey)) {
          newDependencyPairs.push(dep);
          existingPairs.add(pairKey);
        }
      }

      this._dependencies = newDependencyPairs;
      // Refetch issues to get any newly referenced issues and re-process all dependencies
      await this.fetchIssues();
    } catch (error) {
      this._error = 'Failed to expand scan for issue.';
      console.error(error);
    } finally {
      this._loadingDependencies = false;
      this._expandingIssueId = null;
    }
  }

  private async _handleCommitDependency(dependency: DependencyPair) {
    const dependencyKey = `${dependency.source_issue_id}:${dependency.dependent_issue_id}`;
    this._committingDependencies.add(dependencyKey);
    this.requestUpdate();

    try {
      const response = await commitIssueDependencies([dependency]);
      if (response.dependencies.length > 0) {
        // Update the local state to reflect the change
        const committedDep = response.dependencies[0];
        this._dependencies = this._dependencies.map((dep) =>
          dep.source_issue_id === committedDep.source_issue_id &&
          dep.dependent_issue_id === committedDep.dependent_issue_id
            ? { ...dep, is_committed: true }
            : dep
        );
        this._processDependencies();
      }
    } catch (error) {
      console.error('Failed to commit dependency:', error);
      // Optionally, show an error message to the user
    } finally {
      this._committingDependencies.delete(dependencyKey);
      this.requestUpdate(); // Re-render the component
    }
  }

  private async _handleCommitAllDependencies(
    dependencies: DependencyPair[],
    issueId: string
  ) {
    if (dependencies.length === 0) return;

    this._committingAllForIssue = issueId;

    try {
      const response = await commitIssueDependencies(dependencies);
      if (response.dependencies.length > 0) {
        const committedKeys = new Set(
          response.dependencies.map(
            (d) => `${d.source_issue_id}:${d.dependent_issue_id}`
          )
        );

        this._dependencies = this._dependencies.map((dep) => {
          const key = `${dep.source_issue_id}:${dep.dependent_issue_id}`;
          if (committedKeys.has(key)) {
            return { ...dep, is_committed: true };
          }
          return dep;
        });

        this._processDependencies();
      }
    } catch (error) {
      console.error('Failed to commit all dependencies:', error);
    } finally {
      this._committingAllForIssue = null;
    }
  }

  private _handleProjectSelect(e: CustomEvent) {
    this._selectedProjectId = e.target.value;
    this._currentPage = 1;
    this._issues = [];
    this._dependencies = [];
    this._dependencyMap.clear();
    this.fetchIssues();
    this._updateUrl();
  }

  private _openFilterModal() {
    this._isFilterModalOpen = true;
  }

  private _applyFilters(e: CustomEvent) {
    const { status } = e.detail;
    this._selectedStatus = status;
    this._isFilterModalOpen = false;
    this._currentPage = 1; // Reset pagination
    this.fetchIssues();
    this._updateUrl();
  }

  private _goToNextPage() {
    if (this._hasMorePages) {
      this._currentPage++;
      this.fetchIssues();
      this._updateUrl();
    }
  }

  private _goToPreviousPage() {
    if (this._currentPage > 1) {
      this._currentPage--;
      this.fetchIssues();
      this._updateUrl();
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

  private _processDependencies() {
    const newMap = new Map<
      string,
      { blocks: DependencyPair[]; blockedBy: DependencyPair[] }
    >();
    this._issues.forEach((issue) => {
      newMap.set(issue.id, { blocks: [], blockedBy: [] });
    });

    this._dependencies.forEach((dep) => {
      // Add to the 'blocks' list of the source issue
      if (newMap.has(dep.source_issue_id)) {
        newMap.get(dep.source_issue_id)!.blocks.push(dep);
      }
      // Add to the 'blockedBy' list of the dependent issue
      if (newMap.has(dep.dependent_issue_id)) {
        newMap.get(dep.dependent_issue_id)!.blockedBy.push(dep);
      }
    });

    this._dependencyMap = newMap;
  }

  private _renderDependencyDetails(
    deps: { blocks: DependencyPair[]; blockedBy: DependencyPair[] } | undefined,
    issueId: string
  ) {
    if (!deps || (deps.blocks.length === 0 && deps.blockedBy.length === 0)) {
      return html`
        <div class="detail-section">
          <h4>Dependencies</h4>
          <sl-alert variant="primary" open>
            <sl-icon slot="icon" name="info-circle"></sl-icon>
            No dependencies detected for this issue.
          </sl-alert>
        </div>
      `;
    }

    const allDependencies = [...deps.blocks, ...deps.blockedBy];
    const uncommittedDependencies = allDependencies.filter(
      (d) => !d.is_committed && !d.comes_from_tracker
    );

    const renderList = (
      items: DependencyPair[],
      type: 'blocks' | 'blockedBy'
    ) => {
      return html`
        <ul>
          ${items.map((d) => {
            const dependencyKey = `${d.source_issue_id}:${d.dependent_issue_id}`;
            const isCommitting =
              this._committingDependencies.has(dependencyKey);
            const issueId =
              type === 'blocks' ? d.dependent_issue_id : d.source_issue_id;
            const issue = this._issues.find((i) => i.id === issueId);

            return html`
              <li>
                <div class="dependency-info">
                  <strong
                    >${
                      type === 'blocks' ? d.dependency_key : d.issue_key
                    }</strong
                  >: ${issue?.title || 'Unknown Issue'}
                  <div class="dependency-reason">
                    ${d.reason} &bull; Confidence:
                    ${(d.confidence_score * 100).toFixed(0)}%
                  </div>
                </div>
                ${when(
                  !d.comes_from_tracker,
                  () =>
                    html`<sl-button
                      size="small"
                      variant="neutral"
                      @click="${() => this._handleCommitDependency(d)}"
                      ?disabled="${d.is_committed || isCommitting}"
                      .loading="${isCommitting}"
                    >
                      ${d.is_committed ? 'Committed' : 'Commit'}
                    </sl-button>`
                )}
              </li>
            `;
          })}
        </ul>
      `;
    };

    return html`
      <div class="detail-section dependency-details">
        <div class="dependencies-header">
          <h4>Dependencies</h4>
          <sl-button
            size="small"
            variant="primary"
            outline
            ?disabled=${
              uncommittedDependencies.length === 0 ||
              this._committingAllForIssue === issueId
            }
            .loading=${this._committingAllForIssue === issueId}
            @click="${() =>
              this._handleCommitAllDependencies(
                uncommittedDependencies,
                issueId
              )}"
          >
            Commit All
          </sl-button>
        </div>
        ${when(
          deps.blocks.length > 0,
          () => html`
            <h5>Blocks</h5>
            ${renderList(deps.blocks, 'blocks')}
          `
        )}
        ${when(
          deps.blockedBy.length > 0,
          () => html`
            <h5>Blocked By</h5>
            ${renderList(deps.blockedBy, 'blockedBy')}
          `
        )}
      </div>
    `;
  }

  private _renderIssueTable() {
    if (this._loadingIssues && this._issues.length === 0) {
      return html`<div class="loading-container"></div>`;
    }

    if (this._error) {
      return html`<div class="error">${this._error}</div>`;
    }

    if (this._issues.length === 0) {
      return html`
        <sl-alert variant="warning" open>
          <sl-icon slot="icon" name="info-circle"></sl-icon>
          <strong>No Issues Found</strong><br />
          No issues were found for the selected project, or the project is
          empty.
        </sl-alert>
      `;
    }

    return html`
      <sl-card class="table-card">
        <table class="styled-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Title</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            ${this._issues.map((issue) => {
              const deps = this._dependencyMap.get(issue.id);
              const isExpanded = this._expandedRowKey === issue.id;
              return html`
                <tr
                  class="clickable-row ${isExpanded ? 'row-expanded' : ''}"
                  @click=${() => this._toggleRow(issue.id)}
                >
                  <td>
                    <a
                      href="${issue.url}"
                      target="_blank"
                      @click=${(e: Event) => e.stopPropagation()}
                      >${issue.key}</a
                    >
                  </td>
                  <td>
                    ${issue.title}
                    <div class="dependency-tags">
                      ${when(
                        this._loadingDependencies,
                        () =>
                          html`<sl-spinner
                            style="font-size: 1em; vertical-align: middle;"
                          ></sl-spinner>`,
                        () => html`
                          ${when(
                            deps && deps.blocks.length > 0,
                            () => html`
                              <sl-badge
                                pill
                                class="blocks-badge ${
                                  deps.blocks.some((d) => d.comes_from_tracker)
                                    ? 'from-tracker'
                                    : ''
                                }"
                              >
                                <div class="dependency-badge-content">
                                  <sl-tooltip content="Blocks">
                                    <sl-icon name="arrow-right-circle"></sl-icon
                                    >Blocks:
                                  </sl-tooltip>
                                  <span
                                    >${deps?.blocks.map(
                                      (d, i) => html`
                                        <sl-tooltip
                                          content="Reason: ${d.reason} | Confidence: ${(
                                            d.confidence_score * 100
                                          ).toFixed(0)}%"
                                        >
                                          <span
                                            class="${
                                              d.is_committed
                                                ? 'is-committed'
                                                : d.comes_from_tracker
                                                  ? 'from-tracker'
                                                  : ''
                                            }"
                                            >#${
                                              d.dependency_key.match(
                                                /\d+$/
                                              )?.[0]
                                            }</span
                                          > </sl-tooltip
                                        >${
                                          i < deps.blocks.length - 1 ? ', ' : ''
                                        }
                                      `
                                    )}</span
                                  >
                                </div>
                              </sl-badge>
                            `
                          )}
                          ${when(
                            deps && deps.blockedBy.length > 0,
                            () => html`
                              <sl-badge pill class="blocked-by-badge">
                                <div class="dependency-badge-content">
                                  <sl-tooltip content="Blocked by">
                                    <sl-icon name="arrow-left-circle"></sl-icon
                                    >Blocked by:
                                  </sl-tooltip>
                                  <span
                                    >${deps?.blockedBy.map(
                                      (d, i) => html`
                                        <sl-tooltip
                                          content="Reason: ${d.reason} | Confidence: ${(
                                            d.confidence_score * 100
                                          ).toFixed(0)}%"
                                        >
                                          <span
                                            class="${
                                              d.is_committed
                                                ? 'is-committed'
                                                : d.comes_from_tracker
                                                  ? 'from-tracker'
                                                  : ''
                                            }"
                                            >#${
                                              d.issue_key.match(/\d+$/)?.[0]
                                            }</span
                                          > </sl-tooltip
                                        >${
                                          i < deps.blockedBy.length - 1
                                            ? ', '
                                            : ''
                                        }
                                      `
                                    )}</span
                                  >
                                </div>
                              </sl-badge>
                            `
                          )}
                        `
                      )}
                    </div>
                  </td>
                  <td>
                    <sl-badge variant=${getStatusVariant(issue.status)}
                      >${issue.status}</sl-badge
                    >
                  </td>
                  <td>
                    <sl-button
                      size="small"
                      @click=${(e: Event) => {
                        e.stopPropagation();
                        this._expandScanForRow(issue.id);
                      }}
                      ?disabled=${
                        this._loadingDependencies &&
                        this._expandingIssueId !== issue.id
                      }
                      .loading=${this._expandingIssueId === issue.id}
                      variant=${
                        this._expandingIssueId === issue.id
                          ? 'primary'
                          : 'default'
                      }
                      >Expand Scan</sl-button
                    >
                  </td>
                </tr>
                ${
                  isExpanded
                    ? html`
                        <tr class="inline-detail-row">
                          <td colspan="4">
                            <div class="detail-view-card">
                              <single-issue-detail-view .issue=${issue}>
                                <div slot="additional-info">
                                  ${this._renderDependencyDetails(deps, issue.id)}
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
        .loading=${this._loadingIssues}
        @prev-page=${this._goToPreviousPage}
        @next-page=${this._goToNextPage}
      ></pagination-controls>
    `;
  }

  private _updateUrl() {
    if (!window.location.pathname.includes('dependencies')) {
      return;
    }
    const params = new URLSearchParams();
    if (this._selectedProjectId) {
      params.set('project', this._selectedProjectId);
    }
    if (this._selectedStatus) {
      params.set('status', this._selectedStatus);
    }
    if (this._expandedRowKey) {
      params.set('selectedIssue', this._expandedRowKey);
    }
    params.set('page', this._currentPage.toString());
    const newUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.pushState({}, '', newUrl);
  }

  private parseUrlAndUpdateState() {
    const params = new URLSearchParams(window.location.search);
    const projectId = params.get('project');
    if (projectId && this._allProjects.some((p) => p.id === projectId)) {
      this._selectedProjectId = projectId;
    }

    // The rest of the issues area addresses a project by the short id in
    // `?projects=` (Duplicates, Compliance, and the tracker pages that link
    // into them). This page only read `?project=<full uuid>`, so arriving
    // from a tracker landed on whichever project happened to be first and
    // silently showed another project's dependencies. It takes one project,
    // so it takes the first of the listed ones it recognises.
    const shortProjectIds = params.get('projects');
    if (shortProjectIds && this._allProjects.length > 0) {
      const shortIdSet = new Set(
        shortProjectIds
          .split(',')
          .map((value) => value.trim())
          .filter(Boolean)
      );
      const matched = this._allProjects.find((project) =>
        shortIdSet.has(project.id.split('-')[0])
      );
      // No match is not a reason to change the selection: an id from another
      // account, or a stale link, leaves the page where it was.
      if (matched) this._selectedProjectId = matched.id;
    }

    this._expandedRowKey = params.get('selectedIssue') || null;
    this._currentPage = Number(params.get('page')) || 1;
    const status = params.get('status') as IssueStatus | null;
    if (status && ['opened', 'closed', 'all'].includes(status)) {
      this._selectedStatus = status;
    } else {
      this._selectedStatus = 'opened';
    }
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .container {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }
      .table-card {
        width: 100%;
        margin-bottom: var(--sl-spacing-medium);
      }
      .table-card {
        --padding: 0;
        border-spacing: 0;
      }
      .controls {
        display: flex;
        gap: var(--sl-spacing-medium);
        align-items: center;
      }
      sl-select {
        min-width: 250px;
      }
      .dependency-tags {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        padding-top: var(--sl-spacing-3x-small);
      }
      .dependency-tags sl-badge::part(base) {
        cursor: pointer;
      }
      .blocks-badge::part(base) {
        background-color: var(--sl-color-neutral-200);
        color: var(--sl-color-neutral-800);
        border: none;
      }
      .blocked-by-badge::part(base) {
        background-color: var(--sl-color-neutral-300);
        color: var(--sl-color-neutral-800);
        border: none;
      }
      .dependency-badge-content {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-3x-small);
      }
      .clickable-row {
        cursor: pointer;
      }
      .row-expanded {
        background-color: var(--sl-color-primary-50);
      }

      .placeholder-content {
        text-align: center;
      }

      .dependency-details ul {
        list-style-type: none;
        padding-left: var(--sl-spacing-medium);
      }
      .dependency-details li {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: var(--sl-spacing-x-small) 0;
      }
      .dependency-details li:last-child {
        border-bottom: none;
      }
      .dependency-reason {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-500);
      }
      .from-tracker {
        color: var(--sl-color-primary-600);
      }
      .is-committed {
        color: var(--sl-color-success-600);
        font-weight: var(--sl-font-weight-semibold);
      }

      .dependencies-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: var(--sl-spacing-small);
      }

      .dependencies-header h4 {
        margin: 0;
      }
    `,
  ];

  render() {
    return html`
      <project-filter-modal
        .isOpen=${this._isFilterModalOpen}
        .selectedStatus=${this._selectedStatus}
        .showProjects=${false}
        .showResolution=${false}
        @on-close=${() => (this._isFilterModalOpen = false)}
        @on-apply=${this._applyFilters}
      ></project-filter-modal>

      <view-header headerText="Issue Dependencies" width="wide">
        <div slot="main-column" class="controls">
          <sl-select
            placeholder="Select a project..."
            .value=${this._selectedProjectId}
            @sl-change=${this._handleProjectSelect}
            .disabled=${this._allProjects.length === 0}
          >
            ${this._allProjects.map(
              (proj) =>
                html`<sl-option value=${proj.id}>${proj.name}</sl-option>`
            )}
          </sl-select>
          <sl-button @click=${this._openFilterModal}>
            <sl-icon slot="prefix" name="filter"></sl-icon>
            Filter
          </sl-button>
        </div>
      </view-header>

      <div class="column-layout wide">
        <div class="main-column">
          <div class="container">
            ${when(
              this._selectedProjectId,
              () => this._renderIssueTable(),
              () => html`
                <div class="empty-state">
                  <sl-icon
                    name="diagram-3"
                    style="font-size: 3rem; margin-bottom: 1rem;"
                  ></sl-icon>
                  <h2>Select a Project</h2>
                  <p>
                    Please select a project from the dropdown above to view its
                    issues and detect dependencies.
                  </p>
                </div>
              `
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
                  return html`<single-issue-detail-view .issue=${issue}>
                    <div slot="additional-info">
                      ${this._renderDependencyDetails(
                        this._dependencyMap.get(issue.id),
                        issue.id
                      )}
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
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'issues-dependencies-view': IssuesDependenciesView;
  }
}
