import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import {
  listIssueDuplicates,
  checkAIVerdict,
  getIssueDuplicateAiStatus,
  VerdictError,
} from '../api';
import type { DuplicatePair, VerdictState } from '../types';
import { AIModelVerdict, renderVerdict } from '../utils/verdict';
import {
  DEFAULT_FETCH_CONCURRENCY,
  mapWithConcurrency,
} from '../utils/concurrency';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';

@customElement('similar-issues-widget')
export class SimilarIssuesWidget extends LitElement {
  @state() private _topSuggestions: DuplicatePair[] = [];
  @state() private _totalSuggestions = 0;
  @state() private _loading = true;
  @state() private _error: string | null = null;
  @state() private _verdicts: Record<string, VerdictState> = {};

  static styles = css`
    :host {
      display: flex; /* Use flexbox to control child layout */
    }
    ::part(body) {
      padding: 0;
    }
    a {
      color: var(--sl-color-primary-600);
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    sl-alert::part(base),
    sl-card {
      width: 100%;
    }
    sl-card::part(header) {
      background-color: var(--sl-color-neutral-100);
    }
    .suggestion-list {
      list-style: none;
      padding: 0;
      margin: 0;
    }
    .suggestion-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-large);
      border-top: 1px solid var(--sl-color-neutral-200);
    }
    .sub-header {
      font-size: var(--sl-font-size-small);
      padding: var(--sl-spacing-small) var(--sl-spacing-large);
      padding-top: var(--sl-spacing-large);
    }
    .suggestion-item:last-child {
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    .issue-titles {
      font-size: var(--sl-font-size-small);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .verdict-container {
      margin-left: var(--sl-spacing-medium);
      display: inline-flex;
      align-items: center;
    }
    .see-all-container {
      text-align: center;
      padding: var(--sl-spacing-medium);
    }
    .spinner-container {
      display: flex;
      justify-content: center;
      padding: var(--sl-spacing-large);
    }
    .no-model-line {
      font-size: var(--console-text-meta, var(--sl-font-size-small));
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      padding: var(--sl-spacing-small) var(--sl-spacing-large);
    }
    .no-model-line a {
      color: var(--console-link-color, var(--sl-color-primary-600));
    }
  `;

  async connectedCallback() {
    super.connectedCallback();
    this.fetchTopSuggestions();
  }

  async fetchTopSuggestions() {
    this._loading = true;
    try {
      const response = await listIssueDuplicates({ limit: 101 }); // Fetch 101 to check for >100
      const allPairs = response.duplicates;

      allPairs.sort((a, b) => b.similarity - a.similarity);

      this._topSuggestions = allPairs.slice(0, 3);
      this._totalSuggestions = allPairs.length;
      this._error = null;
      await this.fetchAIModelVerdicts();
    } catch (error) {
      console.error('Failed to fetch similar issues:', error);
      this._error = 'Could not load suggestions.';
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

  async fetchAIModelVerdicts() {
    let configured = true;
    try {
      const status = await getIssueDuplicateAiStatus();
      configured = status.configured;
    } catch (error) {
      console.error('Failed to fetch AI status:', error);
    }

    const pairsToFetch = this._topSuggestions.filter(
      (pair) => pair.similarity < 0.999
    );

    if (!configured) {
      const noModel: Record<string, VerdictState> = {};
      for (const pair of pairsToFetch) {
        noModel[this._pairKey(pair)] = { state: 'no_model' };
      }
      this._verdicts = noModel;
      return;
    }

    const initial: Record<string, VerdictState> = {};
    for (const pair of pairsToFetch) {
      initial[this._pairKey(pair)] = { state: 'checking' };
    }
    this._verdicts = initial;

    if (pairsToFetch.length === 0) {
      return;
    }

    await mapWithConcurrency(
      pairsToFetch,
      DEFAULT_FETCH_CONCURRENCY,
      async (pair) => {
        const pairKey = this._pairKey(pair);
        try {
          const verdict = await checkAIVerdict(pair.issue1.id, pair.issue2.id);
          this._setVerdict(pairKey, { state: 'done', verdict });
        } catch (error) {
          console.error(
            `[similar-issues-widget] fetchAIModelVerdicts: API call failed for pair ${pairKey}`,
            error
          );
          if (
            error instanceof VerdictError &&
            error.code === 'no_default_ai_model'
          ) {
            this._setVerdict(pairKey, { state: 'no_model' });
          } else if (
            error instanceof VerdictError &&
            error.code === 'timeout'
          ) {
            this._setVerdict(pairKey, { state: 'timeout' });
          } else {
            this._setVerdict(pairKey, { state: 'failed' });
          }
        }
      }
    );
  }

  private _hasNoModel(): boolean {
    return Object.values(this._verdicts).some(
      (entry) => entry.state === 'no_model'
    );
  }

  private _renderPairVerdict(pair: DuplicatePair) {
    if (pair.similarity > 0.999) {
      return html`<sl-badge
        variant="warning"
        style="--sl-color-warning-text: var(--sl-color-orange-50); --sl-color-warning-600: var(--sl-color-orange-700);"
        pill
        >Identical</sl-badge
      >`;
    }
    const state = this._verdicts[this._pairKey(pair)];
    if (!state || state.state === 'no_model') {
      return html``;
    }
    if (state.state === 'checking') {
      return renderVerdict({ decision: 'checking' });
    }
    if (state.state === 'done') {
      return renderVerdict(state.verdict as AIModelVerdict | undefined);
    }
    return html``;
  }

  render() {
    if (this._loading) {
      return html`
        <sl-card class="table-card">
          <div slot="header">Similar Issue Suggestions</div>
          <div class="spinner-container">
            <sl-spinner></sl-spinner>
          </div>
        </sl-card>
      `;
    }

    if (this._error) {
      return html`
        <sl-alert variant="danger" open>
          <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
          ${this._error}
        </sl-alert>
      `;
    }

    if (this._totalSuggestions === 0) {
      return html`
        <sl-alert variant="primary" open>
          <sl-icon slot="icon" name="info-circle"></sl-icon>
          No similar issues found for the current filters.
        </sl-alert>
      `;
    }

    const renderTopSuggestionsText = () => {
      const count = this._topSuggestions.length;
      if (count === 0) {
        return 'There are no suggestions to display.';
      }
      if (count === 1) {
        return 'Here is the top suggestion:';
      }
      return `Here are the top ${count} suggestions:`;
    };

    return html`
      <sl-card class="table-card">
        <div slot="header">Similar Issue Suggestions</div>
        <div class="sub-header">
          You have
          <a href="/console/issues"
            ><strong
              >${
                this._totalSuggestions > 100 ? '100+' : this._totalSuggestions
              }</strong
            >
            unresolved suggestions</a
          >. ${renderTopSuggestionsText()}
        </div>
        <ul class="suggestion-list">
          ${this._topSuggestions.map((pair) => {
            return html`
              <li class="suggestion-item">
                <div
                  class="issue-titles"
                  title="${pair.issue1.title} vs ${pair.issue2.title}"
                >
                  <strong>${pair.issue1.key}</strong> vs
                  <strong>${pair.issue2.key}</strong>
                </div>
                <div>
                  <sl-badge variant="neutral"
                    >${(pair.similarity * 100).toFixed(0)}%</sl-badge
                  >
                  <div class="verdict-container">
                    ${this._renderPairVerdict(pair)}
                  </div>
                </div>
              </li>
            `;
          })}
        </ul>
        ${
          this._hasNoModel()
            ? html`<div class="no-model-line">
                AI review needs a default model (<a href="/console/ai-models"
                  >Models</a
                >)
              </div>`
            : ''
        }
        ${
          this._totalSuggestions > 0
            ? html`
                <div class="see-all-container">
                  <a href="/console/issues">See all...</a>
                </div>
              `
            : ''
        }
      </sl-card>
    `;
  }
}
