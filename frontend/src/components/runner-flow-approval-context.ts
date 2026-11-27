import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { RunnerFlowApprovalContext } from '../types';

/** "1h 30m" / "45m" / "30s" for a flow's wall-clock limit. */
export function formatTimeLimit(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** "Codex CLI · gpt-x" or whichever half is known. */
export function runnerFlowAgentLabel(
  context: RunnerFlowApprovalContext
): string {
  return [context.harness, context.model].filter(Boolean).join(' · ');
}

/** "laptop · laptop.local · macOS" without repeating the name. */
export function runnerFlowDeviceLabel(
  context: RunnerFlowApprovalContext
): string {
  const name = context.runner?.name || 'your runner';
  const device = context.runner?.device || {};
  const extras = [device.hostname, device.os].filter(
    (value): value is string => !!value && value !== name
  );
  return [name, ...extras].join(' · ');
}

/**
 * What a runner owner is asked to allow: the device, the harness and model,
 * the flow, why it started, what it checks out, whether it can publish, its
 * time limit, and how far the approval reaches. The raw ids the approval
 * binds to stay in the page's collapsed details.
 */
@customElement('runner-flow-approval-context')
export class RunnerFlowApprovalContextBlock extends LitElement {
  @property({ type: Object })
  context: RunnerFlowApprovalContext | null = null;

  /** One line for list rows. */
  @property({ type: Boolean })
  compact = false;

  static styles = css`
    :host {
      display: block;
    }
    dl {
      display: grid;
      grid-template-columns: max-content 1fr;
      gap: 0.5rem 1rem;
      margin: 0;
    }
    dt {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
    dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    ul {
      margin: 0;
      padding-left: 1.1rem;
    }
    .scope {
      margin: 0.75rem 0 0;
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
    }
    .compact {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
    }
  `;

  render() {
    const context = this.context;
    if (!context) return nothing;
    const agent = runnerFlowAgentLabel(context);
    const device = runnerFlowDeviceLabel(context);
    if (this.compact) {
      return html`<span class="compact"
        >${[context.flow?.name, agent, device].filter(Boolean).join(' → ')}</span
      >`;
    }
    const flow = context.flow;
    const timeout = context.limits?.timeout_seconds;
    return html`
      <dl>
        <dt>Runner</dt>
        <dd data-field="runner">${device}</dd>
        ${
          agent
            ? html`<dt>Harness and model</dt>
                <dd data-field="agent">${agent}</dd>`
            : nothing
        }
        ${
          flow?.name
            ? html`<dt>Flow</dt>
                <dd data-field="flow">
                  ${
                    flow.href
                      ? html`<a href=${flow.href}>${flow.name}</a>`
                      : flow.name
                  }
                </dd>`
            : nothing
        }
        ${
          context.trigger?.label
            ? html`<dt>Triggered by</dt>
                <dd data-field="trigger">${context.trigger.label}</dd>`
            : nothing
        }
        ${
          context.repositories?.length
            ? html`<dt>Repositories</dt>
                <dd data-field="repositories">
                  <ul>
                    ${context.repositories.map(
                      (repo) =>
                        html`<li>
                          ${repo.repository}${
                            repo.branch ? html` (${repo.branch})` : nothing
                          }
                        </li>`
                    )}
                  </ul>
                </dd>`
            : nothing
        }
        ${
          context.directory
            ? html`<dt>Directory</dt>
                <dd data-field="directory">${context.directory}</dd>`
            : nothing
        }
        ${
          context.publication?.label
            ? html`<dt>Push and publish</dt>
                <dd data-field="publication">${context.publication.label}</dd>`
            : nothing
        }
        ${
          timeout
            ? html`<dt>Time limit</dt>
                <dd data-field="limits">${formatTimeLimit(timeout)}</dd>`
            : nothing
        }
      </dl>
      ${
        context.scope_label
          ? html`<p class="scope" data-field="scope">${context.scope_label}</p>`
          : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'runner-flow-approval-context': RunnerFlowApprovalContextBlock;
  }
}
