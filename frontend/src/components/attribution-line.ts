import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type {
  ApprovalAgentSummary,
  ApprovalApiKeySummary,
  ApprovalFlowExecutionSummary,
  ApprovalSessionSummary,
} from '../types';
import {
  formatApprovalSource,
  getApprovalSource,
} from '../utils/approval-identity';

/**
 * Who asked: agent, key, session, flow run, each linked to its own page.
 *
 * The problem it solves: an approval raised by a named agent through Agent
 * Control rendered as "Agent: AI agent". The ids were on the record, so the
 * console was printing a generic label for a caller it could identify. An
 * approval is a decision about a specific caller, and a reviewer cannot make
 * it without being able to open the agent, the credential, the session and
 * the run behind it.
 *
 * The rule this component enforces: a part is named when the server named it,
 * shortened to eight characters of its id when only the id is known, and
 * omitted when neither is known. It never invents a label. "AI agent" is not
 * a thing you can click.
 *
 * Clicks are left alone. Vaadin Router listens for clicks on `document` in
 * the bubble phase, so stopping propagation at the anchor would turn every
 * link here into a full page reload. If a surface ever puts this line inside
 * a clickable row, that row should ignore clicks whose `composedPath()`
 * contains an anchor rather than this component muting its own links.
 */

/** Everything the line can read, all of it optional. */
export interface AttributionSource {
  /** Server-resolved summaries (added with the attribution work). */
  agent?: ApprovalAgentSummary | null;
  api_key?: ApprovalApiKeySummary | null;
  session?: ApprovalSessionSummary | null;
  flow_execution?: ApprovalFlowExecutionSummary | null;
  /** Raw ids, still honoured so an older payload keeps its links. */
  managed_agent_id?: string | null;
  managed_agent_name?: string | null;
  api_key_id?: string | null;
  runtime_session_id?: string | null;
  execution_id?: string | null;
  /** Carries `_preloop_source`, the adapter that relayed the call. */
  tool_args?: Record<string, unknown> | null;
}

/** One "Label value" pair, already resolved to what the DOM should show. */
export interface AttributionPart {
  key: 'agent' | 'key' | 'session' | 'flow';
  label: string;
  text: string;
  href?: string;
  /** The full id and the kind, for the hover title on a shortened value. */
  title?: string;
}

/** Eight characters, the console's shortening everywhere ids are shown. */
function shortId(id: string): string {
  return id.slice(0, 8);
}

function clean(value: string | null | undefined): string | null {
  const text = (value || '').trim();
  return text || null;
}

/**
 * The parts of the line, in reading order, with the unknown ones dropped.
 *
 * Exported separately from the element so every surface (and every test) can
 * ask the same question without rendering: what do we actually know about
 * who asked for this?
 */
export function attributionParts(
  source: AttributionSource | null | undefined
): AttributionPart[] {
  if (!source) return [];
  const parts: AttributionPart[] = [];

  const agentId = clean(source.agent?.id) || clean(source.managed_agent_id);
  // The adapter name ("Claude Code") is the last resort, and only when there
  // is no id at all: with an id, the id is the honest answer.
  const adapter = formatApprovalSource(getApprovalSource(source.tool_args));
  const agentName =
    clean(source.agent?.name) ||
    clean(source.managed_agent_name) ||
    (agentId ? shortId(agentId) : adapter);
  if (agentName) {
    const kind = clean(source.agent?.kind);
    parts.push({
      key: 'agent',
      label: 'Agent',
      text: agentName,
      href: agentId ? `/console/agents/${agentId}` : undefined,
      title: [kind, agentId].filter(Boolean).join(' · ') || undefined,
    });
  }

  const keyId = clean(source.api_key?.id) || clean(source.api_key_id);
  const keyName =
    clean(source.api_key?.name) || (keyId ? shortId(keyId) : null);
  if (keyId && keyName) {
    parts.push({
      key: 'key',
      label: 'Key',
      text: keyName,
      href: `/console/settings/api-keys/${keyId}`,
      title: keyId,
    });
  }

  const sessionId =
    clean(source.session?.id) || clean(source.runtime_session_id);
  const sessionName =
    clean(source.session?.subject) || (sessionId ? shortId(sessionId) : null);
  if (sessionId && sessionName) {
    parts.push({
      key: 'session',
      label: 'Session',
      text: sessionName,
      href: `/console/runtime-sessions?sessionId=${encodeURIComponent(
        sessionId
      )}`,
      title: sessionId,
    });
  }

  const runId = clean(source.flow_execution?.id) || clean(source.execution_id);
  const runName =
    clean(source.flow_execution?.flow_name) || (runId ? shortId(runId) : null);
  if (runId && runName) {
    parts.push({
      key: 'flow',
      label: 'Flow run',
      text: runName,
      href: `/console/flows/executions/${runId}`,
      title: runId,
    });
  }

  return parts;
}

@customElement('attribution-line')
export class AttributionLine extends LitElement {
  /** The approval (or anything with the same fields) to attribute. */
  @property({ type: Object })
  source: AttributionSource | null = null;

  /**
   * Drop the labels and keep the links. For a list row, where the column is
   * already "who asked" and four labels would be more words than facts.
   */
  @property({ type: Boolean })
  compact = false;

  static styles = css`
    :host {
      display: block;
    }

    .line {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0 var(--sl-spacing-2x-small);
      font-size: var(--console-text-meta, 0.8125rem);
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      min-width: 0;
    }

    .part {
      display: inline-flex;
      align-items: baseline;
      gap: 0.25rem;
      min-width: 0;
    }

    .label {
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }

    .value {
      color: var(--sl-color-neutral-900);
      max-width: 22ch;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    a.value {
      color: var(--console-link-color, var(--sl-color-primary-600));
      text-decoration: none;
    }

    a.value:hover {
      text-decoration: underline;
    }

    .separator {
      color: var(--console-meta-color, var(--sl-color-neutral-500));
    }
  `;

  render() {
    const parts = attributionParts(this.source);
    // Nothing known is nothing rendered: an empty line is better than a
    // placeholder that claims an agent nobody can open.
    if (!parts.length) return nothing;
    return html`
      <div class="line" part="line">
        ${parts.map(
          (item, index) => html`
            ${
              index > 0
                ? html`<span class="separator" aria-hidden="true">·</span>`
                : nothing
            }
            <span class="part" data-part=${item.key}>
              ${
                this.compact
                  ? nothing
                  : html`<span class="label">${item.label}</span>`
              }
              ${
                item.href
                  ? html`<a
                      class="value"
                      href=${item.href}
                      title=${item.title || item.text}
                      aria-label="${item.label}: ${item.text}"
                      >${item.text}</a
                    >`
                  : html`<span class="value" title=${item.title || item.text}
                      >${item.text}</span
                    >`
              }
            </span>
          `
        )}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'attribution-line': AttributionLine;
  }
}
