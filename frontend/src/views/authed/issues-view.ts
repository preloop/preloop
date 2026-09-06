import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import '@shoelace-style/shoelace/dist/components/button-group/button-group.js';
import '@shoelace-style/shoelace/dist/components/icon-button/icon-button.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import '../../components/project-filter-modal.ts';
import '../../components/resolve-issue-modal.ts';
import '../../components/issue-detail-view.ts';
import '../../components/pagination-controls.ts';
import '../../components/view-header.ts';
import {
  listProjects,
  listIssueDuplicates,
  checkAIVerdict,
  getIssueDuplicateAiStatus,
  dismissDuplicatePair,
  listOrganizations,
  VerdictError,
} from '../../api';
import type {
  Project,
  DuplicatePair,
  DuplicatesResponse,
  Organization,
  VerdictState,
} from '../../types';
import { DEFAULT_SIMILARITY_THRESHOLD } from '../../config';
import {
  AIModelVerdict,
  renderVerdict,
  getStatusVariant,
} from '../../utils/verdict';
import consoleStyles from '../../styles/console-styles.css?inline';

/** The similarity steps the bar offers, coarse enough to be a decision. */
const SIMILARITY_THRESHOLDS = [
  { value: DEFAULT_SIMILARITY_THRESHOLD, label: 'Any similarity' },
  { value: 0.5, label: '50% or more' },
  { value: 0.7, label: '70% or more' },
  { value: 0.8, label: '80% or more' },
  { value: 0.9, label: '90% or more' },
];

/** `open` is how the tracker spells it; "Open" is how the console says it. */
function issueStatusLabel(status: string | null | undefined): string {
  const raw = (status || '').trim();
  if (!raw) return 'Unknown';
  return raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
}

@customElement('issues-view')
export class IssuesView extends LitElement {
  @state()
  private _duplicates: DuplicatePair[] = [];

  @state()
  private _verdicts: Record<string, VerdictState> = {};

  @state()
  private _aiModelName = '';

  @state()
  private _loading = false;

  @state()
  private _error: string | null = null;

  @state()
  private _currentPage = 1;

  @state()
  private _pageSize = 10;

  @state()
  private _hasMorePages = true;

  @state()
  private _expandedRowKey: string | null = null;

  @state()
  private _isFilterModalOpen = false;

  @state()
  private _resolutionSummary: string | null = null;

  @state()
  private _isResolveModalOpen = false;

  @state()
  private _selectedPair: DuplicatePair | null = null;

  @state()
  private _selectedProjectIds: string[] = [];

  @state()
  private _selectedStatus: 'opened' | 'closed' | 'all' = 'opened';

  @state()
  private _selectedResolutionStatus: 'resolved' | 'unresolved' | 'all' = 'all';

  /**
   * How alike two issues have to be before the page suggests the pair.
   *
   * The list used to ask for everything from 10% up, which is why it opened
   * on pages of pairs nobody would call duplicates. It opens on 50% instead,
   * and the bar states the threshold and lets a reader widen it back to
   * everything.
   */
  @state()
  private _similarityThreshold = 0.5;

  @state()
  private _allProjects: Project[] = [];

  @state()
  private _hasProjects = true;

  @state()
  private _organizations: Organization[] = [];

  @state()
  private _initialLoadComplete = false;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .table-card {
        width: 100%;
        --padding: 0;
        border-spacing: 0;
      }

      .styled-table th,
      .styled-table td {
        padding: var(--sl-spacing-medium);
        border-bottom: 1px solid var(--sl-color-neutral-200);
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

      sl-icon {
        font-size: 1rem;
      }

      .placeholder-content {
        text-align: center;
      }

      .issues-toolbar {
        display: flex;
        align-items: end;
        gap: var(--sl-spacing-small);
        justify-content: flex-end;
      }

      .threshold-filter {
        min-width: 170px;
      }
    `,
  ];

  async connectedCallback() {
    super.connectedCallback();
    // Fetch projects first so we can map short IDs from the URL to full IDs.
    await this.fetchProjects();
    this.parseUrlAndUpdateState();
    this.fetchDuplicates();
    this.fetchOrganizations();
    this._initialLoadComplete = true;
    window.addEventListener('popstate', this.handlePopState);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    window.removeEventListener('popstate', this.handlePopState);

    // Check if we are still on the issues path before cleaning up.
    // This prevents a race condition where the URL of the *next* page is cleaned.
    if (window.location.pathname.includes('/issues')) {
      window.history.replaceState({}, '', window.location.pathname);
    }
  }

  private handlePopState = () => {
    this.parseUrlAndUpdateState();
    this.fetchDuplicates();
  };

  private parseUrlAndUpdateState() {
    const params = new URLSearchParams(window.location.search);
    this._currentPage = parseInt(params.get('page') || '1', 10);
    this._selectedStatus = (params.get('status') || 'opened') as
      'opened' | 'closed' | 'all';
    this._selectedResolutionStatus = (params.get('resolution') || 'all') as
      'resolved' | 'unresolved' | 'all';
    const shortProjectIds = params.get('projects');
    if (shortProjectIds && this._allProjects.length > 0) {
      const shortIdSet = new Set(shortProjectIds.split(','));
      this._selectedProjectIds = this._allProjects
        .filter((p) => shortIdSet.has(p.id.split('-')[0]))
        .map((p) => p.id);
    } else {
      this._selectedProjectIds = [];
    }
    this._expandedRowKey = params.get('selectedPair') || null;
  }

  private _updateUrl() {
    // Only update the URL if we are on the issues page.
    if (!window.location.pathname.includes('/issues')) {
      return;
    }

    const params = new URLSearchParams();
    params.set('page', this._currentPage.toString());
    params.set('status', this._selectedStatus);
    if (this._selectedResolutionStatus !== 'all') {
      params.set('resolution', this._selectedResolutionStatus);
    }
    if (this._selectedProjectIds.length > 0) {
      const shortProjectIds = this._selectedProjectIds.map(
        (id) => id.split('-')[0]
      );
      params.set('projects', shortProjectIds.join(','));
    }
    if (this._expandedRowKey) {
      params.set('selectedPair', this._expandedRowKey);
    } else {
      params.delete('selectedPair');
    }

    const newUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.pushState({}, '', newUrl);
  }

  async fetchInitialData() {
    this.fetchDuplicates();
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

  async fetchDuplicates() {
    this._loading = true;
    this._error = null;
    const skip = (this._currentPage - 1) * this._pageSize;

    try {
      const data: DuplicatesResponse = await listIssueDuplicates({
        limit: this._pageSize,
        skip: skip,
        project_ids: this._selectedProjectIds,
        status: this._selectedStatus,
        resolution: this._selectedResolutionStatus,
        similarity_threshold: this._similarityThreshold,
      });

      this._duplicates = data.duplicates;
      this._hasMorePages = data.duplicates.length === this._pageSize;
      this._updateUrl(); // Update URL after fetching
      void this.fetchAIModelVerdicts();
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'An unknown error occurred.';
      console.error('Failed to fetch duplicate issues:', error);
    } finally {
      this._loading = false;
    }
  }

  private _pairKey(pair: DuplicatePair): string {
    return `${pair.issue1.id}-${pair.issue2.id}`;
  }

  private _setVerdict(pairKey: string, next: VerdictState) {
    this._verdicts = { ...this._verdicts, [pairKey]: next };
  }

  private _stateFromError(error: unknown): VerdictState {
    if (error instanceof VerdictError) {
      if (error.code === 'no_default_ai_model') {
        return { state: 'no_model' };
      }
      if (error.code === 'timeout') {
        return { state: 'timeout' };
      }
    }
    return { state: 'failed' };
  }

  async fetchAIModelVerdicts() {
    let configured = true;
    try {
      const status = await getIssueDuplicateAiStatus();
      configured = status.configured;
      this._aiModelName = status.model_name || '';
    } catch (error) {
      console.error('Failed to fetch AI status:', error);
    }

    if (!configured) {
      const noModel: Record<string, VerdictState> = {};
      for (const pair of this._duplicates) {
        noModel[this._pairKey(pair)] = { state: 'no_model' };
      }
      this._verdicts = { ...this._verdicts, ...noModel };
      return;
    }

    const pending = this._duplicates.filter((pair) => {
      const current = this._verdicts[this._pairKey(pair)];
      return !current || current.state === 'checking';
    });

    for (const pair of pending) {
      this._setVerdict(this._pairKey(pair), { state: 'checking' });
    }

    await Promise.all(pending.map((pair) => this._fetchVerdictForPair(pair)));
  }

  private async _fetchVerdictForPair(pair: DuplicatePair) {
    const pairKey = this._pairKey(pair);
    this._setVerdict(pairKey, { state: 'checking' });
    try {
      const verdict = await checkAIVerdict(pair.issue1.id, pair.issue2.id);
      this._setVerdict(pairKey, { state: 'done', verdict });
    } catch (error) {
      console.error(`Failed to fetch AI verdict for ${pairKey}:`, error);
      this._setVerdict(pairKey, this._stateFromError(error));
    }
  }

  private _retryVerdict(pair: DuplicatePair) {
    void this._fetchVerdictForPair(pair);
  }

  private _renderRowVerdict(
    pair: DuplicatePair,
    verdictState: VerdictState | undefined
  ) {
    const state = verdictState?.state;
    if (state === 'no_model') {
      return html`<span>No AI model</span>`;
    }
    if (state === 'failed' || state === 'timeout') {
      return html`
        <sl-button
          size="small"
          variant="text"
          @click=${(e: Event) => {
            e.stopPropagation();
            this._retryVerdict(pair);
          }}
          >Review</sl-button
        >
      `;
    }
    if (state === 'done') {
      return renderVerdict(verdictState?.verdict as AIModelVerdict | undefined);
    }
    return renderVerdict({ decision: 'checking' });
  }

  private _toggleRow(pairKey: string) {
    if (this._expandedRowKey === pairKey) {
      this._expandedRowKey = null;
    } else {
      this._expandedRowKey = pairKey;
    }
    this._updateUrl();
  }

  private _openResolveModal(pair: DuplicatePair) {
    this._selectedPair = pair;
    this._isResolveModalOpen = true;
  }

  private async _handleDismiss(pair: DuplicatePair) {
    // Optimistically remove the pair from the list for a responsive UI
    const pairKey = `${pair.issue1.id}-${pair.issue2.id}`;
    const originalDuplicates = [...this._duplicates];
    this._duplicates = this._duplicates.filter(
      (p) => `${p.issue1.id}-${p.issue2.id}` !== pairKey
    );

    try {
      await dismissDuplicatePair(pair.issue1.id, pair.issue2.id);
    } catch (error) {
      console.error('Failed to dismiss pair:', error);
      // If the API call fails, revert the UI change
      this._duplicates = originalDuplicates;
      // Optionally, show an error toast to the user here
    }
  }

  private _handleModalClose() {
    this._isResolveModalOpen = false;
    this._selectedPair = null;
  }

  private async handleResolution(e: CustomEvent) {
    if (e.detail.summary) {
      this._resolutionSummary = e.detail.summary;
    }
    this.fetchDuplicates();
  }

  private renderDetailRow(pair: DuplicatePair) {
    const pairKey = this._pairKey(pair);
    const verdictState = this._verdicts[pairKey];
    const aiVerdict = verdictState?.verdict as AIModelVerdict | undefined;

    return html`
      <issue-detail-view
        .pair=${pair}
        .aiVerdict=${aiVerdict ?? null}
        .verdictState=${verdictState ?? { state: 'checking' }}
        .modelName=${this._aiModelName}
        @resolve=${() => this._openResolveModal(pair)}
        @dismiss=${() => this._handleDismiss(pair)}
        @retry-verdict=${() => this._retryVerdict(pair)}
      ></issue-detail-view>
    `;
  }

  private _openFilterModal() {
    this._isFilterModalOpen = true;
  }

  private _removeProjectFilter(projectIdToRemove: string) {
    this._selectedProjectIds = this._selectedProjectIds.filter(
      (id) => id !== projectIdToRemove
    );
    this.fetchDuplicates();
  }

  private _clearAllFilters() {
    this._selectedProjectIds = [];
    this.fetchDuplicates();
  }

  private _onThresholdChange(event: Event) {
    const raw = (event.target as { value?: string }).value || '';
    const next = Number(raw);
    if (!Number.isFinite(next) || next === this._similarityThreshold) return;
    this._similarityThreshold = next;
    this._currentPage = 1;
    this.fetchDuplicates();
  }

  private _renderActiveFilters() {
    if (
      this._selectedProjectIds.length === 0 &&
      this._selectedStatus === 'opened' &&
      this._selectedResolutionStatus === 'all'
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
        ${
          this._selectedResolutionStatus !== 'all'
            ? html`
                <sl-tag
                  size="medium"
                  removable
                  @sl-remove=${() => this._clearResolutionFilter()}
                >
                  ${
                    this._selectedResolutionStatus === 'resolved'
                      ? 'Resolved'
                      : 'Unresolved'
                  }
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
    this.fetchDuplicates();
  }

  private _clearResolutionFilter() {
    this._selectedResolutionStatus = 'all';
    this.fetchDuplicates();
  }

  private _goToPreviousPage() {
    if (this._currentPage > 1) {
      this._currentPage--;
      this.fetchDuplicates();
    }
  }

  private _goToNextPage() {
    this._currentPage++;
    this.fetchDuplicates();
  }

  render() {
    return html`
      <view-header
        headerText="Similar issues"
        description="Find overlapping issues and resolve duplicates"
        width="wide"
      >
        <div slot="main-column" class="issues-toolbar">
          <sl-select
            class="threshold-filter"
            size="small"
            label="Similarity"
            .value=${String(this._similarityThreshold)}
            @sl-change=${this._onThresholdChange}
          >
            ${SIMILARITY_THRESHOLDS.map(
              (option) => html`
                <sl-option value=${String(option.value)}
                  >${option.label}</sl-option
                >
              `
            )}
          </sl-select>
          <sl-button size="small" @click=${this._openFilterModal}>
            <sl-icon slot="prefix" name="filter"></sl-icon>
            Filter
          </sl-button>
        </div>
      </view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <div class="container">
            ${when(
              this._resolutionSummary,
              () => html`
                <sl-alert
                  variant="success"
                  open
                  closable
                  @sl-after-hide=${() => (this._resolutionSummary = null)}
                >
                  <sl-icon slot="icon" name="check-circle"></sl-icon>
                  ${this._resolutionSummary}
                </sl-alert>
              `
            )}
            ${this._renderActiveFilters()}
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
              () => html`<div class="error">Error: ${this._error}</div>`
            )}
            ${when(!this._loading && !this._error, () =>
              this._duplicates.length > 0
                ? html`
                    <sl-card class="table-card">
                      <table class="styled-table">
                        <thead>
                          <tr>
                            <th>Issue 1</th>
                            <th>Issue 2</th>
                            <th class="text-right">Similarity</th>
                            <th class="text-right">AI Review</th>
                            <th class="text-right">Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          ${this._duplicates.map((pair) => {
                            const pairKey = this._pairKey(pair);
                            const verdictState = this._verdicts[pairKey];
                            const verdict = verdictState?.verdict as
                              AIModelVerdict | undefined;
                            const isFaint = verdictState?.state === 'checking';
                            const isExpanded = this._expandedRowKey === pairKey;

                            return html`
                              <tr
                                class="clickable-row ${
                                  isFaint ? 'faint-row' : ''
                                } ${isExpanded ? 'row-expanded' : ''}"
                                @click=${() => this._toggleRow(pairKey)}
                              >
                                <td>
                                  <a
                                    href="${
                                      pair.issue1.meta_data?.url ||
                                      pair.issue1.url
                                    }"
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    class="issue-id-link"
                                    @click=${(e: Event) => e.stopPropagation()}
                                  >
                                    <strong class="issue-id"
                                      >${pair.issue1.key}</strong
                                    >
                                    <sl-badge
                                      class="chip"
                                      pill
                                      variant=${getStatusVariant(
                                        pair.issue1.status
                                      )}
                                      >${issueStatusLabel(
                                        pair.issue1.status
                                      )}</sl-badge
                                    >
                                  </a>
                                  <div class="issue-title">
                                    ${pair.issue1.title}
                                  </div>
                                </td>
                                <td>
                                  <a
                                    href="${
                                      pair.issue2.meta_data?.url ||
                                      pair.issue2.url
                                    }"
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    class="issue-id-link"
                                    @click=${(e: Event) => e.stopPropagation()}
                                  >
                                    <strong class="issue-id"
                                      >${pair.issue2.key}</strong
                                    >
                                    <sl-badge
                                      class="chip"
                                      pill
                                      variant=${getStatusVariant(
                                        pair.issue2.status
                                      )}
                                      >${issueStatusLabel(
                                        pair.issue2.status
                                      )}</sl-badge
                                    >
                                  </a>
                                  <div class="issue-title">
                                    ${pair.issue2.title}
                                  </div>
                                </td>
                                <td class="text-right">
                                  ${(pair.similarity * 100).toFixed(2)}%
                                </td>
                                <td
                                  class="text-right"
                                  id="verdict-${pair.issue1.id}-${
                                    pair.issue2.id
                                  }"
                                >
                                  ${
                                    pair.similarity >= 0.999
                                      ? html`<sl-badge
                                          variant="warning"
                                          style="--sl-color-warning-text: var(--sl-color-orange-50); --sl-color-warning-600: var(--sl-color-orange-700);"
                                          >Identical</sl-badge
                                        >`
                                      : this._renderRowVerdict(
                                          pair,
                                          verdictState
                                        )
                                  }
                                </td>
                                <td>
                                  <div class="actions-container">
                                    ${when(
                                      !verdict?.resolution,
                                      () => html`
                                        <sl-button
                                          size="small"
                                          variant="primary"
                                          outline
                                          @click=${(e: Event) => {
                                            e.stopPropagation();
                                            this._openResolveModal(pair);
                                          }}
                                          >Resolve</sl-button
                                        >
                                        <sl-tooltip
                                          content="Dismiss this suggestion"
                                        >
                                          <sl-icon-button
                                            name="x-circle"
                                            label="Dismiss"
                                            @click=${(e: Event) => {
                                              e.stopPropagation();
                                              this._handleDismiss(pair);
                                            }}
                                          ></sl-icon-button>
                                        </sl-tooltip>
                                      `
                                    )}
                                  </div>
                                </td>
                              </tr>
                              ${when(
                                isExpanded,
                                () => html`
                                  <tr class="inline-detail-row">
                                    <td colspan="5">
                                      ${this.renderDetailRow(pair)}
                                    </td>
                                  </tr>
                                `
                              )}
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
                  `
                : html`
                    <sl-alert variant="primary" open>
                      <sl-icon slot="icon" name="info-circle"></sl-icon>
                      ${
                        this._hasProjects
                          ? 'No duplicate issues found for the current filters.'
                          : unsafeHTML(
                              'No projects found. Did you <a href="/console/trackers">add a tracker</a>?'
                            )
                      }
                    </sl-alert>
                  `
            )}
          </div>
        </div>
        <div class="side-column">
          ${when(
            this._expandedRowKey,
            () => {
              const expandedPair = this._duplicates.find(
                (p) => `${p.issue1.id}-${p.issue2.id}` === this._expandedRowKey
              );
              return expandedPair
                ? html`
                    <div class="side-column-detail">
                      <sl-card>
                        <div slot="header">Issue Pair Details</div>
                        ${this.renderDetailRow(expandedPair)}
                      </sl-card>
                    </div>
                  `
                : '';
            },
            () => html`
              <div class="side-column-detail">
                <sl-card class="full-width">
                  <div slot="header">Issue Pair Details</div>
                  <div class="placeholder-content">
                    <sl-icon name="info-circle"></sl-icon>
                    <p>Select an issue pair to see the details here.</p>
                  </div>
                </sl-card>
              </div>
            `
          )}
        </div>
      </div>
      <project-filter-modal
        .isOpen=${this._isFilterModalOpen}
        .allProjects=${this._allProjects}
        .organizations=${this._organizations}
        .projects=${this._allProjects}
        .selectedProjectIds=${this._selectedProjectIds}
        .selectedStatus=${this._selectedStatus}
        .selectedResolution=${this._selectedResolutionStatus}
        @on-close=${() => (this._isFilterModalOpen = false)}
        @on-apply=${this._applyFilters}
      ></project-filter-modal>
      <resolve-issue-modal
        .isOpen=${this._isResolveModalOpen}
        .duplicatePair=${this._selectedPair}
        @on-close=${() => (this._isResolveModalOpen = false)}
        @on-resolved=${this.handleResolution}
      ></resolve-issue-modal>
    `;
  }

  private _applyFilters(event: CustomEvent) {
    this._selectedProjectIds = event.detail.projectIds;
    this._selectedStatus = event.detail.status;
    this._selectedResolutionStatus = event.detail.resolution;
    this._isFilterModalOpen = false;
    this._currentPage = 1; // Reset to first page
    this.fetchDuplicates();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'issues-view': IssuesView;
  }
}
