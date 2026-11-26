/** Search and select the agent's sessions without changing message routing. */
import { LitElement, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dropdown/dropdown.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import { findRuntimeSessions } from '../api';
import type {
  AccountRuntimeSessionListResponse,
  RuntimeSessionSummary,
} from '../types';
import { normalizeObservedSession } from '../utils/session-observer';
import { sessionTree } from '../utils/session-tree';

export const TALK_SESSION_SELECT_EVENT = 'talk-session-select';
export const TALK_SESSION_NEW_EVENT = 'talk-session-new';

@customElement('talk-session-picker')
export class TalkSessionPicker extends LitElement {
  @property({ attribute: false }) sessions: RuntimeSessionSummary[] = [];
  @property({ attribute: false }) currentSessionId: string | null = null;
  @property({ type: String }) agentId = '';
  @property({ type: Boolean }) canStartNew = true;
  @state() private query = '';
  @state() private result: AccountRuntimeSessionListResponse | null = null;
  @state() private loading = false;
  @state() private error = '';
  @state() private expanded = new Set<string>();
  private sequence = 0;
  private debounce?: ReturnType<typeof setTimeout>;

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    clearTimeout(this.debounce);
    this.sequence++;
  }

  public async show(): Promise<void> {
    await this.updateComplete;
    const dropdown = this.querySelector('sl-dropdown') as HTMLElement & {
      show(): Promise<void>;
    };
    await dropdown?.show();
    this.querySelector('sl-input')?.focus();
  }

  private async search(more = false): Promise<void> {
    if (!this.agentId) return;
    const sequence = ++this.sequence;
    this.loading = true;
    this.error = '';
    try {
      const result = await findRuntimeSessions({
        agent: this.agentId,
        query: this.query,
        limit: 30,
        offset: more ? (this.result?.next_offset ?? 0) : 0,
      });
      if (sequence !== this.sequence) return;
      if (more && this.result) {
        result.items = [
          ...new Map(
            [...this.result.items, ...result.items].map((row) => [row.id, row])
          ).values(),
        ];
        result.related_sessions = [
          ...new Map(
            [
              ...(this.result.related_sessions ?? []),
              ...(result.related_sessions ?? []),
            ].map((row) => [row.id, row])
          ).values(),
        ];
        result.matching_snippets = {
          ...this.result.matching_snippets,
          ...result.matching_snippets,
        };
      }
      this.result = result;
      this.error = result.search_warning ?? '';
    } catch (error) {
      if (sequence === this.sequence)
        this.error = error instanceof Error ? error.message : 'Search failed';
    } finally {
      if (sequence === this.sequence) this.loading = false;
    }
  }

  private onInput(event: Event): void {
    this.query = (event.target as HTMLInputElement).value;
    this.sequence++;
    this.result = null;
    this.loading = true;
    clearTimeout(this.debounce);
    this.debounce = setTimeout(() => void this.search(), 250);
  }

  private select(session: RuntimeSessionSummary): void {
    this.dispatchEvent(
      new CustomEvent(TALK_SESSION_SELECT_EVENT, {
        detail: { sessionId: session.id, session },
        bubbles: true,
        composed: true,
      })
    );
    void (
      this.querySelector('sl-dropdown') as HTMLElement & {
        hide(): Promise<void>;
      }
    )?.hide();
  }

  private startNew(): void {
    this.dispatchEvent(
      new CustomEvent(TALK_SESSION_NEW_EVENT, {
        bubbles: true,
        composed: true,
      })
    );
    void (
      this.querySelector('sl-dropdown') as HTMLElement & {
        hide(): Promise<void>;
      }
    )?.hide();
  }

  render() {
    const items = this.result?.items ?? (this.query ? [] : this.sessions);
    const raw = new Map(
      [...items, ...(this.result?.related_sessions ?? [])].map((row) => [
        row.id,
        row,
      ])
    );
    const normalized = [...raw.values()].map(normalizeObservedSession);
    const rows = sessionTree(
      normalized,
      this.expanded,
      new Set([
        ...(this.query ? items.map((row) => row.id) : []),
        ...(this.currentSessionId ? [this.currentSessionId] : []),
      ])
    );
    const group = (status: string): string =>
      status === 'ended'
        ? 'Ended'
        : status === 'active_now'
          ? 'Running'
          : 'Idle';
    let rootGroup = 'Idle';
    const groups = new Map(
      ['Running', 'Idle', 'Ended'].map((label) => [label, [] as typeof rows])
    );
    for (const row of rows) {
      if (row.depth === 0) rootGroup = group(row.session.status);
      groups.get(rootGroup)!.push(row);
    }
    return html` <sl-dropdown
      placement="bottom-end"
      hoist
      @sl-show=${() => void this.search()}
    >
      <sl-button
        slot="trigger"
        size="small"
        caret
        data-testid="talk-session-picker-trigger"
        >Sessions</sl-button
      >
      <div
        class="talk-session-menu"
        style="box-sizing:border-box;width:min(440px, 90vw);max-height:65vh;overflow:auto;padding:var(--sl-spacing-small);background:var(--console-surface, var(--sl-color-neutral-0));border:1px solid var(--console-hairline, var(--sl-color-neutral-200));border-radius:var(--sl-border-radius-medium)"
      >
        <sl-input
          label="Find a session"
          placeholder="Title, prompt, directory or ID"
          .value=${this.query}
          @sl-input=${this.onInput}
        ></sl-input>
        ${this.canStartNew ? html`<sl-button data-testid="talk-new-session" size="small" @click=${this.startNew}>New session</sl-button>` : nothing}
        <div role="status">${this.loading ? 'Searching...' : this.error}</div>
        ${[...groups].map(([label, groupRows]) =>
          groupRows.length
            ? html`
                <div class="meta">${label}</div>
                ${groupRows.map(
                  ({ session, depth, children, runningChildren, expanded }) =>
                    html` <div
                      style=${`margin-inline-start:${Math.min(depth, 6) * 16}px`}
                    >
                      <button
                        type="button"
                        class="talk-session-option"
                        data-session-id=${session.id}
                        aria-current=${session.id === this.currentSessionId ? 'true' : 'false'}
                        style="display:block;width:100%;text-align:start;background:transparent;color:inherit;border:0;padding:8px;cursor:pointer"
                        @click=${() => this.select(raw.get(session.id)!)}
                      >
                        <span class="talk-session-option-title"
                          >${session.title}</span
                        >
                        <span class="talk-session-option-meta"
                          >${session.cwd || session.sessionReference || session.id.slice(0, 8)}</span
                        >
                        ${session.firstPrompt ? html`<span class="talk-session-option-meta" title=${session.firstPrompt}>${session.firstPrompt.slice(0, 120)}</span>` : nothing}
                        ${session.parentSessionId ? html`<span class="talk-session-option-meta">Subagent of ${normalized.find((row) => row.id === session.parentSessionId)?.title || session.parentSessionId.slice(0, 8)} · ${group(session.status)}</span>` : nothing}
                        ${this.result?.matching_snippets?.[session.id] ? html`<span class="talk-session-option-meta">${this.result.matching_snippets[session.id].replace(/<\/?mark>/g, '')}</span>` : nothing}
                      </button>
                      ${
                        children
                          ? html`<sl-button
                              size="small"
                              variant="text"
                              aria-expanded=${expanded}
                              @click=${() => {
                                const next = new Set(this.expanded);
                                if (next.has(session.id))
                                  next.delete(session.id);
                                else next.add(session.id);
                                this.expanded = next;
                              }}
                              >${expanded ? 'Hide' : 'Show'} ${children}
                              subagents${runningChildren ? ` · ${runningChildren} running` : ''}</sl-button
                            >`
                          : nothing
                      }
                    </div>`
                )}
              `
            : nothing
        )}
        ${!this.loading && !this.error && !items.length ? html`<p>No sessions matched. Searched titles, first prompts, directories, agents, IDs and captured content.</p>` : nothing}
        ${this.result?.has_more ? html`<sl-button size="small" ?loading=${this.loading} @click=${() => void this.search(true)}>Load more sessions</sl-button>` : nothing}
        ${this.result?.related_truncated ? html`<p>Some related sessions are omitted. Search by title or ID to find them.</p>` : nothing}
      </div>
    </sl-dropdown>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'talk-session-picker': TalkSessionPicker;
  }
}
