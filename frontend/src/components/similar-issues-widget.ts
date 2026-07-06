import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { listIssueDuplicates, checkAIVerdict } from '../api';
import type { DuplicatePair } from '../types';
import { AIModelVerdict, renderVerdict } from '../utils/verdict';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';

@customElement('similar-issues-widget')
export class SimilarIssuesWidget extends LitElement {
  @state() private _topSuggestions: DuplicatePair[] = [];
  @state() private _totalSuggestions = 0;
  @state() private _loading = true;
  @state() private _error: string | null = null;
  @state() private _aiVerdicts: Record<string, AIModelVerdict> = {};

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

  async fetchAIModelVerdicts() {
    // 1. Set all to 'checking' and update the UI once.
    const initialVerdicts: Record<string, AIModelVerdict> = {};
    const pairsToFetch: DuplicatePair[] = [];

    for (const pair of this._topSuggestions) {
      if (pair.similarity < 0.999) {
        const pairKey = `${pair.issue1.id}-${pair.issue2.id}`;
        initialVerdicts[pairKey] = { decision: 'checking' };
        pairsToFetch.push(pair);
      }
    }
    this._aiVerdicts = initialVerdicts;

    // 2. Create an array of promises for all the API calls.
    const verdictPromises = pairsToFetch.map((pair) =>
      checkAIVerdict(pair.issue1.id, pair.issue2.id).catch((error) => {
        console.error(
          `[similar-issues-widget] fetchAIModelVerdicts: API call failed for pair ${pair.issue1.id}-${pair.issue2.id}`,
          error
        );
        // Return a specific error object so Promise.all doesn't fail completely
        return { error: true, pairKey: `${pair.issue1.id}-${pair.issue2.id}` };
      })
    );

    if (verdictPromises.length === 0) {
      return;
    }

    // 3. Wait for all promises to settle.
    const results = await Promise.all(verdictPromises);

    // 4. Process the results and update the state a final time.
    const finalVerdicts = { ...this._aiVerdicts };
    results.forEach((result, index) => {
      const pair = pairsToFetch[index];
      const pairKey = `${pair.issue1.id}-${pair.issue2.id}`;

      if (result.error) {
        finalVerdicts[pairKey] = {
          decision: 'undecided',
          reason: 'Failed to load',
        };
      } else {
        finalVerdicts[pairKey] = {
          decision: result.decision || 'undecided',
          reason: result.reason,
        };
      }
    });

    this._aiVerdicts = finalVerdicts;
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
            const pairKey = `${pair.issue1.id}-${pair.issue2.id}`;
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
                    ${
                      pair.similarity > 0.999
                        ? html`<sl-badge
                            variant="warning"
                            style="--sl-color-warning-text: var(--sl-color-orange-50); --sl-color-warning-600: var(--sl-color-orange-700);"
                            pill
                            >Identical</sl-badge
                          >`
                        : renderVerdict(this._aiVerdicts[pairKey])
                    }
                  </div>
                </div>
              </li>
            `;
          })}
        </ul>
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
