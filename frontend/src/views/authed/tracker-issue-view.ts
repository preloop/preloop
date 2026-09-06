import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '../../components/view-header.ts';
import { fetchWithAuth, getIssue } from '../../api';
import { openRunPresetDialog } from '../../components/run-preset-dialog';
import type { IssueListItem } from '../../types';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { renderMarkdown, markdownBodyCss } from '../../utils/markdown';
import { getStatusVariant } from '../../utils/verdict';
import consoleStyles from '../../styles/console-styles.css?inline';

interface TrackerSummary {
  id: string;
  name: string;
  tracker_type: string;
}

@customElement('tracker-issue-view')
export class TrackerIssueView extends LitElement {
  @state()
  private _loading = true;

  @state()
  private _error: string | null = null;

  @state()
  private _issue: IssueListItem | null = null;

  @state()
  private _tracker: TrackerSummary | null = null;

  private _trackerId = '';
  private _issueId = '';

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .breadcrumb {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-2x-small);
        align-items: center;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
        margin-bottom: var(--sl-spacing-small);
      }

      .breadcrumb a {
        color: var(--console-link-color);
        text-decoration: none;
      }

      .breadcrumb a:hover {
        text-decoration: underline;
      }

      /* One line of facts under the title: key, state, project, freshness. */
      .meta-row {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-medium);
        align-items: center;
        margin-bottom: var(--sl-spacing-medium);
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }

      .meta-key {
        font-family: var(--sl-font-mono);
      }

      .meta-row a {
        color: var(--console-link-color);
        text-decoration: none;
      }

      .meta-row a:hover {
        text-decoration: underline;
      }

      .header-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: var(--sl-spacing-small);
      }

      .issue-empty-body {
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
      }
    `,
    unsafeCSS(markdownBodyCss),
  ];

  connectedCallback() {
    super.connectedCallback();
    const params = (this as { location?: { params?: Record<string, string> } })
      .location?.params;
    this._trackerId = params?.trackerId || '';
    this._issueId = params?.issueId || '';
    void this._load();
  }

  private _trackerLabel(): string {
    if (!this._tracker) return 'tracker';
    const type = this._tracker.tracker_type?.toLowerCase() || '';
    if (type.includes('gitlab')) return 'GitLab';
    if (type.includes('jira')) return 'Jira';
    return 'GitHub';
  }

  /** `open` is a database value; "Open" is what the meta line says. */
  private _statusLabel(status: string | null | undefined): string {
    const raw = (status || '').trim();
    if (!raw) return 'Unknown';
    return raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
  }

  private _isGitTracker(): boolean {
    const type = this._tracker?.tracker_type?.toLowerCase() || '';
    return type.includes('github') || type.includes('gitlab');
  }

  private _runImplementer() {
    if (!this._issue) return;
    void openRunPresetDialog({
      presetSlug: 'automated-issue-implementation',
      target: { kind: 'issue', issue_id: this._issue.id },
      issueKey: this._issue.key,
      role: 'implementer',
    });
  }

  private _runTriage() {
    if (!this._issue) return;
    void openRunPresetDialog({
      presetSlug: 'issue-triage-assistant',
      target: { kind: 'issue', issue_id: this._issue.id },
      issueKey: this._issue.key,
      role: 'triage',
    });
  }

  private _similarIssuesHref(): string {
    const projectId = this._issue?.project_id || '';
    const shortId = projectId.split('-')[0];
    return shortId ? `/console/issues?projects=${shortId}` : '/console/issues';
  }

  private async _load() {
    this._loading = true;
    this._error = null;
    try {
      const [issue, trackerRes] = await Promise.all([
        getIssue(this._issueId),
        fetchWithAuth(`/api/v1/trackers/${this._trackerId}`),
      ]);
      this._issue = issue;
      this._tracker = trackerRes.ok
        ? ((await trackerRes.json()) as TrackerSummary)
        : null;
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Failed to load issue';
    } finally {
      this._loading = false;
    }
  }

  render() {
    if (this._loading) {
      return html`
        <div class="spinner-container">
          <sl-spinner></sl-spinner>
        </div>
      `;
    }

    if (this._error || !this._issue) {
      return html`
        <view-header headerText="Issue" width="narrow">
          <div slot="top">
            <sl-button
              variant="text"
              size="small"
              href="/console/trackers/${this._trackerId}"
            >
              <sl-icon slot="prefix" name="arrow-left"></sl-icon>
              Back to tracker
            </sl-button>
          </div>
        </view-header>
        <div class="column-layout narrow">
          <sl-alert variant="danger" open>
            ${this._error || 'Issue not found'}
          </sl-alert>
        </div>
      `;
    }

    const issue = this._issue;
    const trackerName = this._tracker?.name || 'Tracker';
    const labels = (issue.labels || []).filter((label) => label);
    const body = renderMarkdown(issue.description);

    return html`
      <view-header headerText=${issue.title} width="narrow">
        <div slot="top" class="breadcrumb">
          <a href="/console/trackers">Trackers</a>
          <span>/</span>
          <a href="/console/trackers/${this._trackerId}">${trackerName}</a>
          <span>/</span>
          <span>${issue.key}</span>
        </div>
        <div slot="main-column" class="header-actions">
          <sl-button size="small" @click=${() => this._runTriage()}>
            Run triage
          </sl-button>
          ${
            this._isGitTracker()
              ? html`
                  <sl-button
                    size="small"
                    variant="primary"
                    @click=${() => this._runImplementer()}
                  >
                    Run implementer
                  </sl-button>
                `
              : nothing
          }
          <sl-button
            size="small"
            variant="text"
            href=${this._similarIssuesHref()}
          >
            Find similar issues
          </sl-button>
        </div>
      </view-header>
      <div class="column-layout narrow" style="padding-top: 0;">
        <div class="main-column">
          <div class="meta-row">
            <span class="meta-key">${issue.key}</span>
            <sl-badge
              class="chip"
              pill
              variant=${getStatusVariant(issue.status || '')}
              >${this._statusLabel(issue.status)}</sl-badge
            >
            <span>${issue.project}</span>
            <span title=${formatLocalDateTime(issue.updated_at)}
              >Updated ${formatRelativeTime(issue.updated_at)}</span
            >
            ${issue.priority ? html`<span>Priority ${issue.priority}</span>` : ''}
            ${issue.assignee ? html`<span>Assignee ${issue.assignee}</span>` : ''}
            ${labels.map(
              (label) =>
                html`<sl-badge class="chip" pill variant="neutral"
                  >${label}</sl-badge
                >`
            )}
            ${
              issue.url
                ? html`<a href=${issue.url} target="_blank" rel="noreferrer"
                    >Open in ${this._trackerLabel()}</a
                  >`
                : ''
            }
          </div>
          ${
            body
              ? html`<div class="markdown-body">${unsafeHTML(body)}</div>`
              : html`<p class="issue-empty-body">
                  No description on this issue.
                </p>`
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'tracker-issue-view': TrackerIssueView;
  }
}
