import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '../../components/view-header.ts';
import '../../components/resource-actions.ts';
import {
  fetchWithAuth,
  getFeatures,
  deleteTracker,
  listIssues,
  listOrganizations,
  listProjectPullRequests,
  listProjects,
  syncTracker,
  type FeaturesResponse,
} from '../../api';
import { openRunPresetDialog } from '../../components/run-preset-dialog';
import type {
  IssueListItem,
  Organization,
  Project,
  PullRequestListItem,
} from '../../types';
import {
  describeTrackerScope,
  groupProjectsByOrganization,
} from '../../utils/tracker-scope';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { confirmDialog } from '../../components/confirm-dialog';
import { trackerKindLabel } from '../../components/tracker-list';
import { getStatusVariant } from '../../utils/verdict';
import consoleStyles from '../../styles/console-styles.css?inline';

/** Where the last project read on a tracker is remembered, per session. */
const PROJECT_MEMORY_KEY = 'preloop.tracker.project.';

/** How many projects we will count open issues for to pick a default. */
const OPEN_COUNT_PROBE_LIMIT = 8;

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

  /** Open issues per project, read once to pick the default project. */
  private _openIssueCounts = new Map<string, number>();

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

  @state()
  private _activeTab: 'projects' | 'issues' | 'pull-requests' = 'projects';

  @state()
  private _selectedProjectId = '';

  @state()
  private _issueStatus: 'open' | 'closed' | 'all' = 'open';

  @state()
  private _issueSearch = '';

  @state()
  private _issues: IssueListItem[] = [];

  @state()
  private _issuesTotal = 0;

  @state()
  private _issuesSkip = 0;

  @state()
  private _issuesLoading = false;

  @state()
  private _issuesError: string | null = null;

  @state()
  private _selectedIssueIds: string[] = [];

  @state()
  private _pullRequests: PullRequestListItem[] = [];

  @state()
  private _prsPage = 1;

  @state()
  private _prsHasMore = false;

  @state()
  private _prsLoading = false;

  @state()
  private _prsError: string | null = null;

  private _trackerId = '';
  private readonly _issuesPageSize = 20;
  private readonly _triageBatchMax = 25;
  private _issuesRequestId = 0;
  private _issueSearchTimer: number | null = null;
  private readonly _prsPageSize = 20;

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
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--console-hairline);
        font-size: var(--console-text-body);
      }

      .project-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small);
        flex-shrink: 0;
      }

      .project-row:last-child {
        border-bottom: none;
      }

      sl-tab-group::part(tabs) {
        border-bottom: 1px solid var(--console-hairline);
      }

      sl-tab::part(base) {
        font-size: var(--console-text-body);
      }

      .issues-controls {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: end;
      }

      .issues-controls sl-select,
      .issues-controls sl-input {
        min-width: 160px;
      }

      .select-col {
        width: 2.5rem;
      }

      .issues-empty,
      .issues-error,
      .prs-empty,
      .prs-error {
        padding: var(--sl-spacing-medium) 0;
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
      }

      .live-note,
      .pr-branches {
        font-size: 13px;
        color: var(--console-meta-color);
      }

      .live-note {
        margin: 0 0 var(--sl-spacing-small) 0;
      }

      .load-more {
        display: block;
        margin-top: var(--sl-spacing-small);
        font-size: var(--console-text-meta);
      }

      .visually-hidden {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
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
        padding: var(--sl-spacing-medium) 0;
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
        line-height: 1.5;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    this._trackerId = (this as any).location?.params?.trackerId || '';
    this._readUrl();
    this._loadData();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._issueSearchTimer !== null) {
      window.clearTimeout(this._issueSearchTimer);
      this._issueSearchTimer = null;
    }
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
    this._projects = allProjects
      .filter((project) => orgIds.has(project.organization_id))
      .sort((left, right) => (left.name || '').localeCompare(right.name || ''));
    // The probe costs one request per project, and it only decides which
    // project the Issues tab opens on. A URL that names one, a project read
    // earlier this session, or a tab that does not list issues all answer
    // the question for free, so none of them pays for it.
    if (
      this._activeTab === 'issues' &&
      !this._selectedProjectId &&
      !this._projectMatching(this._rememberedProjectId())
    ) {
      await this._loadOpenIssueCounts();
    }
    this._ensureSelectedProject();
    // The address bar states which project the tab opened on, so a reload or
    // a shared link lands on the same one rather than picking again.
    this._updateUrl();
    if (this._activeTab === 'issues') {
      await this._loadIssues(true);
    } else if (this._activeTab === 'pull-requests') {
      await this._loadPullRequests(true);
    }
  }

  private _readUrl() {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get('tab');
    if (tab === 'issues') {
      this._activeTab = 'issues';
    } else if (tab === 'pull-requests') {
      this._activeTab = 'pull-requests';
    } else {
      this._activeTab = 'projects';
    }
    const status = params.get('status');
    if (status === 'open' || status === 'closed' || status === 'all') {
      this._issueStatus = status;
    }
    this._selectedProjectId = params.get('project') || '';
  }

  private _updateUrl() {
    if (!window.location.pathname.includes('/console/trackers/')) {
      return;
    }
    const params = new URLSearchParams();
    if (this._activeTab === 'issues') {
      params.set('tab', 'issues');
      if (this._selectedProjectId) {
        params.set('project', this._selectedProjectId);
      }
      params.set('status', this._issueStatus);
    } else if (this._activeTab === 'pull-requests') {
      params.set('tab', 'pull-requests');
      if (this._selectedProjectId) {
        params.set('project', this._selectedProjectId);
      }
    }
    const query = params.toString();
    const next = query
      ? `${window.location.pathname}?${query}`
      : window.location.pathname;
    window.history.replaceState({}, '', next);
  }

  /**
   * How many open issues each project has, for the default selection only.
   *
   * The count endpoint is one cheap request per project (limit 1, read the
   * total), so this asks for a handful and gives up quietly on the rest: a
   * default is worth one round trip, not twenty.
   */
  private async _loadOpenIssueCounts(): Promise<void> {
    const projects = this._projects.slice(0, OPEN_COUNT_PROBE_LIMIT);
    if (projects.length < 2) return;
    const counts = new Map<string, number>();
    await Promise.all(
      projects.map(async (project) => {
        try {
          const page = await listIssues({
            project_id: project.id,
            status: 'open',
            skip: 0,
            limit: 1,
          });
          counts.set(project.id, Number(page.total) || 0);
        } catch {
          // A project whose count we could not read simply does not win.
        }
      })
    );
    this._openIssueCounts = counts;
  }

  /**
   * Which project the tabs open on.
   *
   * A URL that names one wins. Otherwise the tracker remembers the last one
   * read this session, and failing that the tab opens on the project with
   * the most open issues: landing on an empty project because its name
   * starts with "A" is how the page taught people it was broken.
   */
  private _ensureSelectedProject() {
    if (this._projects.length === 0) {
      this._selectedProjectId = '';
      return;
    }
    const requested = this._selectedProjectId || this._rememberedProjectId();
    const match = this._projectMatching(requested);
    if (match) {
      this._selectedProjectId = match.id;
      // A link followed once is what this tracker is being read on, so the
      // next visit without the query lands on it too.
      this._rememberProject(match.id);
      return;
    }
    const busiest = [...this._projects].sort(
      (left, right) =>
        (this._openIssueCounts.get(right.id) || 0) -
        (this._openIssueCounts.get(left.id) || 0)
    )[0];
    this._selectedProjectId = busiest ? busiest.id : this._projects[0].id;
    this._rememberProject(this._selectedProjectId);
  }

  /** The loaded project an id names, by full id or by its short form. */
  private _projectMatching(requested: string): Project | undefined {
    if (!requested) return undefined;
    return this._projects.find(
      (project) =>
        project.id === requested ||
        this._shortProjectId(project.id) === requested
    );
  }

  /** The project this tracker was last read on, per tab session. */
  private _rememberedProjectId(): string {
    if (!this._trackerId) return '';
    try {
      return sessionStorage.getItem(PROJECT_MEMORY_KEY + this._trackerId) || '';
    } catch {
      return '';
    }
  }

  private _rememberProject(projectId: string): void {
    if (!this._trackerId || !projectId) return;
    try {
      sessionStorage.setItem(PROJECT_MEMORY_KEY + this._trackerId, projectId);
    } catch {
      // A browser with storage disabled just does not remember.
    }
  }

  private _selectedProject(): Project | undefined {
    return this._projects.find(
      (project) => project.id === this._selectedProjectId
    );
  }

  private async _loadIssues(reset = true) {
    if (!this._selectedProjectId) {
      this._issues = [];
      this._issuesTotal = 0;
      return;
    }
    const requestId = ++this._issuesRequestId;
    if (reset) {
      this._issuesSkip = 0;
      this._issues = [];
      this._selectedIssueIds = [];
    }
    this._issuesLoading = true;
    this._issuesError = null;
    const q = this._issueSearch.trim();
    try {
      const data = await listIssues({
        project_id: this._selectedProjectId,
        status: this._issueStatus,
        q: q || undefined,
        skip: this._issuesSkip,
        limit: this._issuesPageSize,
        sort: 'updated_desc',
      });
      if (requestId !== this._issuesRequestId) {
        return;
      }
      this._issues = reset ? data.items : [...this._issues, ...data.items];
      this._issuesTotal = data.total;
      this._issuesSkip = data.skip;
    } catch (error) {
      if (requestId !== this._issuesRequestId) {
        return;
      }
      this._issuesError =
        error instanceof Error ? error.message : 'Could not load issues.';
    } finally {
      if (requestId === this._issuesRequestId) {
        this._issuesLoading = false;
      }
    }
  }

  private _visibleIssues(): IssueListItem[] {
    return this._issues;
  }

  private _showTab(name: 'projects' | 'issues' | 'pull-requests') {
    this._activeTab = name;
    const group = this.renderRoot.querySelector('sl-tab-group') as {
      show?: (panel: string) => void;
    } | null;
    group?.show?.(name);
  }

  private _onTabShow(event: CustomEvent<{ name: string }>) {
    const name = event.detail.name;
    if (name !== 'projects' && name !== 'issues' && name !== 'pull-requests') {
      return;
    }
    if (this._activeTab === name) return;
    this._activeTab = name;
    this._updateUrl();
    if (name === 'issues') {
      void this._loadIssues(true);
    } else if (name === 'pull-requests') {
      void this._loadPullRequests(true);
    }
  }

  private _showIssuesForProject(projectId: string) {
    this._selectedProjectId = projectId;
    this._rememberProject(projectId);
    this._showTab('issues');
    this._updateUrl();
    void this._loadIssues(true);
  }

  private _showPullRequestsForProject(projectId: string) {
    this._selectedProjectId = projectId;
    this._rememberProject(projectId);
    this._showTab('pull-requests');
    this._updateUrl();
    void this._loadPullRequests(true);
  }

  private _onProjectFilter(event: Event) {
    const value = (event.target as HTMLSelectElement).value;
    if (!value || value === this._selectedProjectId) return;
    this._selectedProjectId = value;
    this._rememberProject(value);
    this._updateUrl();
    if (this._activeTab === 'pull-requests') {
      void this._loadPullRequests(true);
    } else {
      void this._loadIssues(true);
    }
  }

  private _onStatusFilter(event: Event) {
    const value = (event.target as HTMLSelectElement).value as
      'open' | 'closed' | 'all';
    this._issueStatus = value;
    this._updateUrl();
    void this._loadIssues(true);
  }

  private _onIssueSearch(event: Event) {
    this._issueSearch = (event.target as HTMLInputElement).value;
    if (this._issueSearchTimer !== null) {
      window.clearTimeout(this._issueSearchTimer);
    }
    this._issueSearchTimer = window.setTimeout(() => {
      this._issueSearchTimer = null;
      void this._loadIssues(true);
    }, 250);
  }

  private _loadMoreIssues() {
    this._issuesSkip = this._issues.length;
    void this._loadIssues(false);
  }

  private _isGitTracker(): boolean {
    const type = this._tracker?.tracker_type?.toLowerCase() || '';
    return type.includes('github') || type.includes('gitlab');
  }

  private _runImplementer(issue: IssueListItem) {
    void openRunPresetDialog({
      presetSlug: 'automated-issue-implementation',
      target: { kind: 'issue', issue_id: issue.id },
      issueKey: issue.key,
      role: 'implementer',
    });
  }

  private _selectedVisibleIssues(): IssueListItem[] {
    const selected = new Set(this._selectedIssueIds);
    return this._visibleIssues().filter((issue) => selected.has(issue.id));
  }

  private _toggleIssueSelection(issueId: string, checked: boolean) {
    if (checked) {
      if (this._selectedIssueIds.includes(issueId)) return;
      if (this._selectedIssueIds.length >= this._triageBatchMax) return;
      this._selectedIssueIds = [...this._selectedIssueIds, issueId];
      return;
    }
    this._selectedIssueIds = this._selectedIssueIds.filter(
      (id) => id !== issueId
    );
  }

  private _toggleSelectVisible(checked: boolean) {
    if (!checked) {
      this._selectedIssueIds = [];
      return;
    }
    const visibleIds = this._visibleIssues().map((issue) => issue.id);
    this._selectedIssueIds = visibleIds.slice(0, this._triageBatchMax);
  }

  private _runTriageOnSelected() {
    const selected = this._selectedVisibleIssues();
    if (selected.length === 0) return;
    void openRunPresetDialog({
      presetSlug: 'issue-triage-assistant',
      targets: selected.map((issue) => ({
        kind: 'issue' as const,
        issue_id: issue.id,
      })),
      issueKey:
        selected.length === 1 ? selected[0].key : `${selected.length} issues`,
      role: 'triage',
    });
  }

  private _isGitlab(): boolean {
    return (this._tracker?.tracker_type || '').toLowerCase().includes('gitlab');
  }

  private _isJira(): boolean {
    return (this._tracker?.tracker_type || '').toLowerCase().includes('jira');
  }

  private _supportsPullRequests(): boolean {
    return !this._isJira();
  }

  private _prNoun(): string {
    return this._isGitlab() ? 'merge requests' : 'pull requests';
  }

  private _prTabLabel(): string {
    return this._isGitlab() ? 'Merge requests' : 'Pull requests';
  }

  private _prHost(): string {
    return this._isGitlab() ? 'GitLab' : 'GitHub';
  }

  private async _loadPullRequests(reset = true) {
    if (!this._selectedProjectId) {
      this._pullRequests = [];
      this._prsHasMore = false;
      return;
    }
    if (reset) {
      this._prsPage = 1;
      this._pullRequests = [];
    }
    this._prsLoading = true;
    this._prsError = null;
    try {
      const data = await listProjectPullRequests(this._selectedProjectId, {
        state: 'open',
        page: this._prsPage,
        limit: this._prsPageSize,
      });
      if (!data.supported) {
        this._pullRequests = [];
        this._prsHasMore = false;
        return;
      }
      this._pullRequests = reset
        ? data.items
        : [...this._pullRequests, ...data.items];
      this._prsHasMore = data.has_more;
    } catch (error) {
      this._prsError =
        error instanceof Error ? error.message : 'Could not reach tracker.';
    } finally {
      this._prsLoading = false;
    }
  }

  private _loadMorePullRequests() {
    this._prsPage += 1;
    void this._loadPullRequests(false);
  }

  private _retryPullRequests() {
    void this._loadPullRequests(this._pullRequests.length === 0);
  }

  private _renderPrsError() {
    return html`<div class="prs-error">
      Could not reach ${this._prHost()}.
      <sl-button
        size="small"
        variant="text"
        @click=${() => this._retryPullRequests()}
        >Retry</sl-button
      >
    </div>`;
  }

  private _renderPrBranches(pr: PullRequestListItem) {
    const source = pr.source_branch || '';
    const target = pr.target_branch || '';
    if (!source || !target) {
      return html`<td class="pr-branches"></td>`;
    }
    return html`<td class="pr-branches" aria-label="${source} to ${target}">
      ${source}
      <span aria-hidden="true"> -> </span>
      <span class="visually-hidden">to</span>
      ${target}
    </td>`;
  }

  private _runReviewer(pr: PullRequestListItem) {
    void openRunPresetDialog({
      presetSlug: 'pull-request-reviewer',
      target: {
        kind: 'pull_request',
        project_id: this._selectedProjectId,
        number: pr.number,
      },
      issueKey: `#${pr.number}`,
      role: 'reviewer',
    });
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
      if (
        this._activeTab === 'pull-requests' &&
        !this._supportsPullRequests()
      ) {
        this._activeTab = 'projects';
        this._updateUrl();
      }
      if (this._activeTab === 'issues') {
        this._showTab('issues');
      } else if (this._activeTab === 'pull-requests') {
        this._showTab('pull-requests');
      }
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
    const confirmed = await confirmDialog({
      title: 'Delete tracker',
      message: `Delete the tracker "${this._tracker.name}"?`,
      detail:
        'Flows that trigger on it stop firing. The issues themselves stay in the tracker.',
      confirmLabel: 'Delete tracker',
      variant: 'danger',
    });
    if (!confirmed) return;

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

  private _renderIssuesTab() {
    if (this._projects.length === 0) {
      return html`<div class="no-projects">
        No projects synced yet. Run <strong>Sync Now</strong> or edit the
        tracker to choose groups and projects, then sync again.
      </div>`;
    }

    const project = this._selectedProject();
    const visible = this._visibleIssues();
    const canLoadMore = this._issues.length < this._issuesTotal;

    return html`
      <sl-card>
        <div slot="header" class="section-header">
          <h2 class="section-title">
            Issues
            ${
              this._issuesTotal
                ? html`<sl-badge variant="neutral" pill class="solid"
                    >${this._issuesTotal}</sl-badge
                  >`
                : ''
            }
          </h2>
          <div class="issues-controls">
            <sl-select
              size="small"
              label="Project"
              value=${this._selectedProjectId}
              @sl-change=${this._onProjectFilter}
            >
              ${this._projects.map(
                (item) =>
                  html`<sl-option value=${item.id}>${item.name}</sl-option>`
              )}
            </sl-select>
            <sl-select
              size="small"
              label="Status"
              value=${this._issueStatus}
              @sl-change=${this._onStatusFilter}
            >
              <sl-option value="open">Open</sl-option>
              <sl-option value="closed">Closed</sl-option>
              <sl-option value="all">All</sl-option>
            </sl-select>
            <sl-input
              size="small"
              label="Search"
              placeholder="Key or title"
              value=${this._issueSearch}
              @sl-input=${this._onIssueSearch}
            ></sl-input>
            <sl-button
              size="small"
              ?disabled=${this._selectedIssueIds.length === 0}
              data-testid="run-triage-selected"
              @click=${() => this._runTriageOnSelected()}
            >
              Run triage on selected
              ${
                this._selectedIssueIds.length
                  ? html`(${this._selectedIssueIds.length})`
                  : nothing
              }
            </sl-button>
          </div>
        </div>
        ${
          this._issuesError
            ? html`<div class="issues-error">
                Could not load issues.
                <sl-button
                  size="small"
                  variant="text"
                  @click=${() => this._loadIssues(true)}
                  >Retry</sl-button
                >
              </div>`
            : this._issuesLoading && this._issues.length === 0
              ? html`<div class="spinner-container">
                  <sl-spinner></sl-spinner>
                </div>`
              : visible.length === 0
                ? html`<div class="issues-empty">
                    ${
                      this._issueSearch.trim()
                        ? `No issues match '${this._issueSearch.trim()}'.`
                        : this._issueStatus === 'open'
                          ? `No open issues in ${project?.name || 'this project'}. Switch the status filter to see closed issues.`
                          : `No issues in ${project?.name || 'this project'}.`
                    }
                  </div>`
                : html`
                    <table class="styled-table">
                      <thead>
                        <tr>
                          <th class="select-col">
                            <sl-checkbox
                              ?checked=${
                                this._visibleIssues().length > 0 &&
                                this._selectedVisibleIssues().length ===
                                  Math.min(
                                    this._visibleIssues().length,
                                    this._triageBatchMax
                                  )
                              }
                              ?indeterminate=${
                                this._selectedVisibleIssues().length > 0 &&
                                this._selectedVisibleIssues().length <
                                  Math.min(
                                    this._visibleIssues().length,
                                    this._triageBatchMax
                                  )
                              }
                              @sl-change=${(event: Event) => {
                                const checkbox = event.target as {
                                  checked?: boolean;
                                };
                                this._toggleSelectVisible(
                                  Boolean(checkbox.checked)
                                );
                              }}
                            >
                              <span class="visually-hidden">Select issues</span>
                            </sl-checkbox>
                          </th>
                          <th>Key</th>
                          <th>Title</th>
                          <th>Status</th>
                          <th>Updated</th>
                          <th>
                            <span class="visually-hidden">Actions</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        ${visible.map(
                          (issue) => html`
                            <tr>
                              <td class="select-col">
                                <sl-checkbox
                                  ?checked=${this._selectedIssueIds.includes(
                                    issue.id
                                  )}
                                  ?disabled=${
                                    !this._selectedIssueIds.includes(
                                      issue.id
                                    ) &&
                                    this._selectedIssueIds.length >=
                                      this._triageBatchMax
                                  }
                                  @sl-change=${(event: Event) => {
                                    const checkbox = event.target as {
                                      checked?: boolean;
                                    };
                                    this._toggleIssueSelection(
                                      issue.id,
                                      Boolean(checkbox.checked)
                                    );
                                  }}
                                >
                                  <span class="visually-hidden"
                                    >Select ${issue.key}</span
                                  >
                                </sl-checkbox>
                              </td>
                              <td>
                                <a
                                  href="/console/trackers/${this._trackerId}/issues/${issue.id}"
                                >
                                  ${issue.key}
                                </a>
                              </td>
                              <td>${issue.title}</td>
                              <td>
                                <sl-badge
                                  pill
                                  variant=${getStatusVariant(
                                    issue.status || ''
                                  )}
                                  >${issue.status}</sl-badge
                                >
                              </td>
                              <td title=${issue.updated_at}>
                                ${formatRelativeTime(issue.updated_at)}
                              </td>
                              <td>
                                ${
                                  this._isGitTracker()
                                    ? html`
                                        <sl-button
                                          size="small"
                                          @click=${() =>
                                            this._runImplementer(issue)}
                                          >Run implementer</sl-button
                                        >
                                      `
                                    : nothing
                                }
                              </td>
                            </tr>
                          `
                        )}
                      </tbody>
                    </table>
                    ${
                      canLoadMore
                        ? html`<sl-button
                            class="load-more"
                            size="small"
                            variant="text"
                            ?loading=${this._issuesLoading}
                            @click=${() => this._loadMoreIssues()}
                            >Load more</sl-button
                          >`
                        : ''
                    }
                  `
        }
      </sl-card>
    `;
  }

  private _renderPullRequestsTab() {
    if (this._projects.length === 0) {
      return html`<div class="no-projects">
        No projects synced yet. Run <strong>Sync Now</strong> or edit the
        tracker to choose groups and projects, then sync again.
      </div>`;
    }

    const project = this._selectedProject();
    const heading = this._isGitlab()
      ? 'Open merge requests'
      : 'Open pull requests';
    const empty = `No open ${this._prNoun()} in ${project?.name || 'this project'}.`;
    const errorHost = this._prHost();

    return html`
      <sl-card>
        <div slot="header" class="section-header">
          <h2 class="section-title">${heading}</h2>
          <div class="issues-controls">
            <sl-select
              size="small"
              label="Project"
              value=${this._selectedProjectId}
              @sl-change=${this._onProjectFilter}
            >
              ${this._projects.map(
                (item) =>
                  html`<sl-option value=${item.id}>${item.name}</sl-option>`
              )}
            </sl-select>
          </div>
        </div>
        <p class="live-note">Live from ${errorHost}, refreshed every minute.</p>
        ${
          this._prsLoading && this._pullRequests.length === 0
            ? html`<div class="spinner-container">
                <sl-spinner></sl-spinner>
              </div>`
            : this._pullRequests.length === 0
              ? this._prsError
                ? this._renderPrsError()
                : html`<div class="prs-empty">${empty}</div>`
              : html`
                  <table class="styled-table">
                    <thead>
                      <tr>
                        <th>Number</th>
                        <th>Title</th>
                        <th>Author</th>
                        <th>Branches</th>
                        <th>Updated</th>
                        <th>
                          <span class="visually-hidden">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      ${this._pullRequests.map(
                        (pr) => html`
                          <tr>
                            <td>
                              <a
                                href=${pr.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                >#${pr.number}</a
                              >
                            </td>
                            <td>${pr.title}</td>
                            <td>${pr.author || ''}</td>
                            ${this._renderPrBranches(pr)}
                            <td title=${pr.updated_at || ''}>
                              ${
                                pr.updated_at
                                  ? formatRelativeTime(pr.updated_at)
                                  : ''
                              }
                            </td>
                            <td>
                              <sl-button
                                size="small"
                                @click=${() => this._runReviewer(pr)}
                                >Run reviewer</sl-button
                              >
                            </td>
                          </tr>
                        `
                      )}
                    </tbody>
                  </table>
                  ${
                    this._prsHasMore
                      ? html`<sl-button
                          class="load-more"
                          size="small"
                          variant="text"
                          ?loading=${this._prsLoading}
                          @click=${() => this._loadMorePullRequests()}
                          >Load more</sl-button
                        >`
                      : ''
                  }
                  ${this._prsError ? this._renderPrsError() : ''}
                `
        }
      </sl-card>
    `;
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
                    <div class="project-actions">
                      <sl-button
                        size="small"
                        variant="text"
                        @click=${() => this._showIssuesForProject(project.id)}
                      >
                        Issues
                      </sl-button>
                      ${
                        this._supportsPullRequests()
                          ? html`<sl-button
                              size="small"
                              variant="text"
                              @click=${() =>
                                this._showPullRequestsForProject(project.id)}
                            >
                              ${this._prTabLabel()}
                            </sl-button>`
                          : ''
                      }
                    </div>
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
    // Relative in the line, exact in the title: "2d ago" is the fact an
    // operator is checking, a date is what they check it against.
    const createdDate = formatRelativeTime(tracker.created);
    const updatedDate = formatRelativeTime(tracker.last_updated);
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
                label: 'Sync now',
                icon: 'arrow-repeat',
                loading: this._syncing,
                disabled: this._syncing,
                onClick: () => this.handleSync(),
              },
              // Destructive last, outlined and pushed away from the rest,
              // so Delete is never the button next to Edit (DESIGN.md,
              // "Destructive actions").
              {
                id: 'delete',
                label: 'Delete',
                icon: 'trash',
                variant: 'danger',
                outline: true,
                separated: true,
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
            <sl-badge
              class="chip"
              variant=${tracker.is_valid ? 'success' : 'warning'}
              pill
              >${tracker.is_valid ? 'Connected' : 'Not validated'}</sl-badge
            >
          </div>

          <div class="tracker-meta">
            <span>
              <sl-icon name="tag"></sl-icon>
              ${trackerKindLabel(tracker.tracker_type)}
            </span>
            <span title=${formatLocalDateTime(tracker.created)}>
              <sl-icon name="calendar3"></sl-icon>
              Created ${createdDate}
            </span>
            <span title=${formatLocalDateTime(tracker.last_updated)}>
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

          <sl-tab-group @sl-tab-show=${this._onTabShow}>
            <sl-tab
              slot="nav"
              panel="projects"
              ?active=${this._activeTab === 'projects'}
              >Projects</sl-tab
            >
            <sl-tab
              slot="nav"
              panel="issues"
              ?active=${this._activeTab === 'issues'}
              >Issues</sl-tab
            >
            ${
              this._supportsPullRequests()
                ? html`<sl-tab
                    slot="nav"
                    panel="pull-requests"
                    ?active=${this._activeTab === 'pull-requests'}
                    >${this._prTabLabel()}</sl-tab
                  >`
                : ''
            }
            <sl-tab-panel name="projects">
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
              </div>
              ${this._renderProjectGroups()}
            </sl-tab-panel>
            <sl-tab-panel name="issues">
              ${this._renderIssuesTab()}
            </sl-tab-panel>
            ${
              this._supportsPullRequests()
                ? html`<sl-tab-panel name="pull-requests">
                    ${this._renderPullRequestsTab()}
                  </sl-tab-panel>`
                : ''
            }
          </sl-tab-group>

          <sl-divider></sl-divider>

          <div class="section-header">
            <h2 class="section-title">Issue analytics</h2>
            ${
              hasProjects
                ? html`<sl-button
                    variant="text"
                    size="small"
                    href=${this._buildIssuesUrl('')}
                  >
                    Similar issues
                  </sl-button>`
                : ''
            }
          </div>

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
