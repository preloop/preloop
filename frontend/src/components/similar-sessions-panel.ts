import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import type {
  SimilarityBand,
  SimilarSessionMatch,
  SimilarSessionResult,
  SimilarSessionsResponse,
} from '../types';
import { formatRelativeTime } from '../utils/date';

/**
 * Wording for each similarity band.
 *
 * The API publishes a cosine number and a band; the console shows the band.
 * A number between zero and one reads as a measurement an operator can trust
 * to two decimal places, and it is not one: it is a distance in whichever
 * vector space the account's embedding model happens to define. The number is
 * still in the title attribute for anyone who wants it.
 */
const BAND_LABELS: Record<SimilarityBand, string> = {
  close: 'Close',
  related: 'Related',
  loose: 'Loosely related',
};

const BAND_VARIANTS: Record<SimilarityBand, string> = {
  close: 'primary',
  related: 'neutral',
  loose: 'neutral',
};

/** Sentences for the reason codes this panel can be handed. */
const REASON_SENTENCES: Record<string, string> = {
  semantic_not_enabled: 'Session embedding is off for this account.',
  semantic_disabled: 'Session embedding is off in this deployment.',
  semantic_model_mismatch:
    'This session was embedded with a different model than the one configured now.',
  semantic_backfill_incomplete: 'Some of this session is still being indexed.',
  similar_session_not_embedded: 'This session has nothing indexed to compare.',
  similar_no_comparable_sessions:
    'No other session of this account is indexed in the same vector space.',
  similar_session_sampled:
    'Only part of this session was compared, sampled evenly across it.',
  similar_window_applied: 'Only recent sessions were compared.',
};

/**
 * Sessions similar to the one on screen.
 *
 * The panel does no fetching of its own. It asks, once, when the operator
 * opens it, by dispatching `similar-sessions-requested`; the host owns the
 * request the same way it owns every other session read. Nothing is fetched
 * for a session nobody expanded, because this list is a side question and
 * should not cost a vector query on every session anyone clicks.
 *
 * Every entry is a link with a real `href`, so a middle click opens the other
 * session in a tab. A plain left click asks the host first with a cancelable
 * `similar-session-selected`: a host that can switch sessions in place calls
 * `preventDefault()` and keeps the operator where they are.
 */
@customElement('similar-sessions-panel')
export class SimilarSessionsPanel extends LitElement {
  /** The session being compared from. Used only for link building. */
  @property({ type: String, attribute: 'runtime-session-id' })
  runtimeSessionId = '';

  @property({ type: Object })
  response: SimilarSessionsResponse | null = null;

  @property({ type: Boolean })
  loading = false;

  /** A failed request, said in one sentence rather than thrown away. */
  @property({ type: String })
  error = '';

  /** Opens the panel already expanded, for a host that wants it eager. */
  @property({ type: Boolean })
  open = false;

  /**
   * Whether the host has already been asked for this session.
   *
   * Deliberately not reactive: it changes nothing on screen, and a reactive
   * flag set from `updated()` schedules a second render for nothing.
   */
  private requested = false;

  static styles = css`
    :host {
      display: block;
    }

    sl-details::part(base) {
      border-radius: var(--sl-border-radius-medium);
    }

    .summary {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-2x-small);
      font-weight: 600;
    }

    .count {
      color: var(--sl-color-neutral-500);
      font-weight: 400;
    }

    .note {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin-bottom: var(--sl-spacing-small);
    }

    .entries {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-x-small);
    }

    a.entry {
      display: block;
      padding: var(--sl-spacing-x-small);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      color: inherit;
      text-decoration: none;
    }

    a.entry:hover,
    a.entry:focus-visible {
      border-color: var(--sl-color-primary-400);
      background: var(--sl-color-neutral-50);
    }

    .entry-head {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-x-small);
      justify-content: space-between;
    }

    .entry-title {
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .entry-meta {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
      margin-top: 2px;
    }

    .match {
      margin-top: var(--sl-spacing-2x-small);
      padding-left: var(--sl-spacing-x-small);
      border-left: 2px solid var(--sl-color-neutral-200);
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
    }

    .match-turn {
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
    }

    .loading {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-x-small);
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
  `;

  /**
   * Ask the host for the list the first time this is opened.
   *
   * Idempotent on purpose: an operator collapsing and reopening the panel is
   * not asking for a second query.
   */
  private requestOnce(): void {
    if (this.requested || !this.runtimeSessionId) return;
    this.requested = true;
    this.dispatchEvent(
      new CustomEvent('similar-sessions-requested', {
        detail: { runtimeSessionId: this.runtimeSessionId },
        bubbles: true,
        composed: true,
      })
    );
  }

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('runtimeSessionId')) {
      // A different session is a different question, so the next expand asks
      // again rather than showing the previous session's neighbours.
      this.requested = false;
    }
    if (this.open) this.requestOnce();
  }

  /** Where an entry points when the host does not take the click. */
  private hrefFor(result: SimilarSessionResult): string {
    return `/console/sessions?sessionId=${encodeURIComponent(
      result.runtime_session_id
    )}&replay=conversation`;
  }

  private onEntryClick(event: MouseEvent, result: SimilarSessionResult): void {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      // A modified click is a request for a new tab. Leave it alone.
      return;
    }
    const handledInPlace = !this.dispatchEvent(
      new CustomEvent('similar-session-selected', {
        detail: {
          runtimeSessionId: result.runtime_session_id,
          matchDocumentId: result.matches[0]?.document_id ?? null,
          matchSourceId: result.matches[0]?.source_id ?? null,
          matchOccurredAt: result.matches[0]?.occurred_at ?? null,
        },
        bubbles: true,
        composed: true,
        cancelable: true,
      })
    );
    if (handledInPlace) event.preventDefault();
  }

  /** The degraded block as one sentence, or nothing when all of it ran. */
  private degradedSentence(): string {
    const degraded = this.response?.degraded;
    if (!degraded || !degraded.reasons.length) return '';
    if (degraded.detail) return degraded.detail;
    return degraded.reasons
      .map((reason) => REASON_SENTENCES[reason] || reason)
      .join(' ');
  }

  private renderMatch(match: SimilarSessionMatch) {
    const turn = match.occurred_at
      ? formatRelativeTime(match.occurred_at)
      : 'this session';
    return html`
      <div class="match">
        <div class="match-turn">
          ${match.role ? `${match.role} · ` : ''}${turn}
        </div>
        ${
          match.text
            ? html`<div>${match.text}</div>`
            : html`<div class="match-turn">
                Content withheld by this account's redaction settings.
              </div>`
        }
      </div>
    `;
  }

  private renderResult(result: SimilarSessionResult) {
    const label =
      result.title ||
      result.session_reference ||
      result.session_source_id ||
      result.runtime_session_id;
    const started = result.started_at
      ? formatRelativeTime(result.started_at)
      : '';
    return html`
      <a
        class="entry"
        href=${this.hrefFor(result)}
        @click=${(event: MouseEvent) => this.onEntryClick(event, result)}
      >
        <div class="entry-head">
          <span class="entry-title" title=${label}>${label}</span>
          <sl-badge
            variant=${BAND_VARIANTS[result.band]}
            pill
            title="Cosine similarity ${result.similarity.toFixed(2)}"
          >
            ${BAND_LABELS[result.band]}
          </sl-badge>
        </div>
        <div class="entry-meta">
          ${started ? `Started ${started} · ` : ''}${result.matched_chunk_count}
          matching passage${result.matched_chunk_count === 1 ? '' : 's'}
        </div>
        ${result.matches.map((match) => this.renderMatch(match))}
      </a>
    `;
  }

  private renderBody() {
    if (this.loading) {
      return html`<div class="loading">
        <sl-spinner></sl-spinner>
        <span>Comparing this session...</span>
      </div>`;
    }
    if (this.error) {
      return html`<div class="note">${this.error}</div>`;
    }
    if (!this.response) {
      return html`<div class="note">Open to compare this session.</div>`;
    }
    const sentence = this.degradedSentence();
    const results = this.response.results;
    return html`
      ${
        results.length
          ? nothing
          : html`<div class="note">No similar session found.</div>`
      }
      ${sentence ? html`<div class="note">${sentence}</div>` : nothing}
      <div class="entries">
        ${results.map((result) => this.renderResult(result))}
      </div>
    `;
  }

  render() {
    const count = this.response?.results.length ?? 0;
    return html`
      <sl-details
        ?open=${this.open}
        @sl-show=${() => this.requestOnce()}
        data-testid="similar-sessions-details"
      >
        <div class="summary" slot="summary">
          <sl-icon name="diagram-3"></sl-icon>
          <span>Similar sessions</span>
          ${this.response ? html`<span class="count">${count}</span>` : nothing}
        </div>
        ${this.renderBody()}
      </sl-details>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'similar-sessions-panel': SimilarSessionsPanel;
  }
}
