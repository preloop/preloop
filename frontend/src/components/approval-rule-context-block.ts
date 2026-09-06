import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { ApprovalRuleContext } from '../types';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';

/**
 * "Why this needs approval": the policy rule that gated a tool call.
 *
 * The problem it solves: an approver used to see the tool name and the
 * argument values but not which rule fired, so a call sitting exactly on a
 * threshold looked identical to one in the middle of a band, and a
 * mis-scoped rule was invisible at the one moment someone could catch it.
 *
 * It reports what matched and nothing more. There is no risk score here and
 * no recommendation: an expression evaluated true, that is the whole claim.
 *
 * Renders nothing when there is no rule context. Approvals raised without
 * rule evaluation (the `request_approval` builtin) and rows created before
 * the field existed legitimately have none, and inventing an explanation for
 * them would be worse than showing none.
 */
@customElement('approval-rule-context-block')
export class ApprovalRuleContextBlock extends LitElement {
  /** The snapshot taken when the approval was created. */
  @property({ type: Object })
  ruleContext: ApprovalRuleContext | null = null;

  /**
   * Compact mode for list rows and other tight surfaces: rule name only, no
   * expression. A clipped expression reads as a different rule, so it is
   * omitted rather than truncated.
   */
  @property({ type: Boolean })
  compact = false;

  /**
   * The tool this approval is for, used to narrow the Tools page when the
   * reviewer follows the link. There is no per-tool route to deep-link to
   * yet, so the next best thing is to not make them scroll a catalogue.
   */
  @property({ type: String })
  toolName: string | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .rule-block {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-left: 3px solid var(--sl-color-primary-500);
      border-radius: var(--sl-border-radius-medium);
      padding: 0.875rem 1rem;
    }

    .rule-heading {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-weight: 600;
      font-size: 0.9rem;
      color: var(--sl-color-neutral-900);
      margin-bottom: 0.5rem;
    }

    .rule-name {
      font-weight: 600;
      color: var(--sl-color-neutral-900);
    }

    .rule-expression {
      font-family: var(--sl-font-mono);
      font-size: 0.8125rem;
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-small);
      padding: 0.5rem 0.625rem;
      margin-top: 0.5rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--sl-color-neutral-900);
    }

    .rule-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.375rem;
      margin-top: 0.5rem;
    }

    .rule-explanation {
      font-size: 0.85rem;
      color: var(--sl-color-neutral-700);
      line-height: 1.5;
    }

    .rule-link {
      display: inline-block;
      margin-top: 0.625rem;
      font-size: 0.8125rem;
      color: var(--sl-color-primary-700);
    }

    .compact {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.8125rem;
      color: var(--sl-color-neutral-700);
    }

    .compact sl-icon {
      color: var(--sl-color-primary-600);
    }
  `;

  /** True when this context names a real rule with an expression. */
  /**
   * An expression worth printing under the name.
   *
   * Unnamed rules are recorded with their expression as the name, and
   * printing "args.amount >= 100" as the heading and again in the mono block
   * underneath reads as two facts when it is one. The compact variant
   * already makes the same test against the rule id.
   */
  private get hasExpression(): boolean {
    const context = this.ruleContext;
    return Boolean(
      context?.expression && context.expression !== context.rule_name
    );
  }

  /** Tools page, narrowed to this tool when we know which one it is. */
  private get toolsHref(): string {
    return this.toolName
      ? `/console/tools#tool=${encodeURIComponent(this.toolName)}`
      : '/console/tools';
  }

  private detectorHint(context: ApprovalRuleContext): string {
    const summary = context.detector_summary;
    if (!summary) {
      return '';
    }
    const parts: string[] = [];
    if (summary['pii.found']) {
      parts.push('PII');
    }
    if (typeof summary['injection.score'] === 'number') {
      parts.push(`injection ${summary['injection.score']}`);
    }
    if (summary['moderation.flagged']) {
      parts.push('moderation');
    }
    return parts.join(', ');
  }

  private renderCompact(context: ApprovalRuleContext) {
    const detectorHint = this.detectorHint(context);
    const title = [context.expression, detectorHint]
      .filter(Boolean)
      .join(' · ');
    const showRuleId =
      Boolean(context.rule_id) && context.rule_id !== context.rule_name;
    return html`
      <span class="compact" title=${title}>
        <sl-icon name="funnel"></sl-icon>
        <span
          >${context.rule_name}${
            showRuleId ? ` (${context.rule_id})` : ''
          }${detectorHint ? ` · ${detectorHint}` : ''}</span
        >
      </span>
    `;
  }

  render() {
    const context = this.ruleContext;
    if (!context || !context.rule_name) {
      // No recorded reason. Say nothing rather than guess.
      return nothing;
    }

    if (this.compact) {
      return this.renderCompact(context);
    }

    const referenced = context.referenced_args ?? [];
    const alsoMatched = context.also_matched_rule_ids ?? [];

    return html`
      <div class="rule-block" part="block">
        <div class="rule-heading">
          <sl-icon name="funnel"></sl-icon>
          <span>Why this needs approval</span>
        </div>

        <div class="rule-name">${context.rule_name}</div>
        ${
          context.rule_id
            ? html`<div class="rule-explanation">
                Rule id: ${context.rule_id}
              </div>`
            : nothing
        }
        ${
          context.explanation
            ? html`<div class="rule-explanation">${context.explanation}</div>`
            : nothing
        }
        ${
          context.detector_summary
            ? html`<div class="rule-expression">
                ${Object.entries(context.detector_summary)
                  .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
                  .join(' ')}
              </div>`
            : nothing
        }
        ${
          this.hasExpression
            ? html`<div class="rule-expression">${context.expression}</div>`
            : nothing
        }
        ${
          referenced.length > 0 ||
          alsoMatched.length > 0 ||
          context.priority !== undefined
            ? html`
                <div class="rule-meta">
                  ${
                    context.priority !== undefined
                      ? html`<sl-tag size="small" variant="neutral"
                          >Priority ${context.priority}</sl-tag
                        >`
                      : nothing
                  }
                  ${referenced.map(
                    (name) =>
                      html`<sl-tag size="small" variant="primary"
                        >Checks ${name}</sl-tag
                      >`
                  )}
                  ${
                    alsoMatched.length > 0
                      ? html`<sl-tag size="small" variant="warning"
                          >${alsoMatched.length} lower-priority
                          ${alsoMatched.length === 1 ? 'rule' : 'rules'} also
                          matched</sl-tag
                        >`
                      : nothing
                  }
                </div>
              `
            : nothing
        }
        ${
          context.source === 'model_io_rule'
            ? html`<a class="rule-link" href="/console/policies"
                >Review this rule in Policies</a
              >`
            : context.tool_configuration_id
              ? html`<a class="rule-link" href=${this.toolsHref}
                  >Review this rule in Tools</a
                >`
              : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'approval-rule-context-block': ApprovalRuleContextBlock;
  }
}
