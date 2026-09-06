import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
// The component renders a tooltip, so it registers one. Every page that
// shows token figures would otherwise depend on some other module in its
// import graph having registered it first.
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import type { GatewayTokenUsage } from '../types';

/**
 * The console's one way to state token volume.
 *
 * Lists used to lead with cost and, where they stated tokens at all, stated
 * one opaque total. A total cannot answer the two questions an operator
 * actually has: is this spend long inputs or long outputs, and is the input
 * being served from the prompt cache. So every list leads with tokens, split
 * in and out, and says how much of the input hit the cache.
 *
 * Compact per DESIGN.md "Numbers": counts under 1000 whole, at or above 1000
 * compact (`12.4K`), units named once ("12.4K in - 3.1K out"), tabular
 * numerals inherited from the shell. The exact figures live in the tooltip
 * and in `title`, so a screen reader and a hover get the same sentence.
 */

/** Compact count per DESIGN.md: whole under 1000, `12.4K` at or above. */
export function formatTokenCount(value: number | null | undefined): string {
  const amount = Number(value || 0);
  if (!Number.isFinite(amount)) return '0';
  if (Math.abs(amount) < 1000) return String(Math.round(amount));
  return new Intl.NumberFormat(undefined, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount);
}

/** Exact count with thousands separators, for tooltips and `title`. */
export function formatExactTokenCount(
  value: number | null | undefined
): string {
  return new Intl.NumberFormat().format(Math.round(Number(value || 0)));
}

/**
 * Whole-percent cache hit rate, or null when the rate is unknown.
 *
 * Unknown is not zero: a provider that reports no cache fields has not told
 * us that nothing hit, so the cache segment is omitted rather than printing
 * "0% hit" over traffic nobody measured.
 */
export function formatCacheHitRate(
  ratio: number | null | undefined
): string | null {
  if (ratio === null || ratio === undefined) return null;
  const value = Number(ratio);
  if (!Number.isFinite(value)) return null;
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

/** The cache read/miss counts an aggregate reports, or null when it reports none. */
export function cacheSplitOf(
  usage: GatewayTokenUsage | null | undefined
): { hit: number; miss: number; ratio: number | null } | null {
  if (!usage) return null;
  const hit = Number(usage.cache_read_tokens || 0);
  const miss = Number(usage.uncached_input_tokens || 0);
  const ratio =
    usage.cache_hit_ratio === null || usage.cache_hit_ratio === undefined
      ? hit + miss > 0
        ? hit / (hit + miss)
        : null
      : Number(usage.cache_hit_ratio);
  if (ratio === null) return null;
  return { hit, miss, ratio };
}

/** Input tokens, reading whichever name the endpoint used. */
export function inputTokensOf(
  usage: GatewayTokenUsage | null | undefined
): number {
  return Number(usage?.input_tokens ?? usage?.prompt_tokens ?? 0);
}

/** Output tokens, reading whichever name the endpoint used. */
export function outputTokensOf(
  usage: GatewayTokenUsage | null | undefined
): number {
  return Number(usage?.completion_tokens ?? usage?.output_tokens ?? 0);
}

/**
 * Add up several aggregates into one.
 *
 * Counts add; a rate does not, so the combined hit rate is recomputed from
 * the combined counts and stays null while nothing reported a cache split.
 * Returns null when there is nothing to state at all.
 */
export function sumTokenUsage(
  items: Array<GatewayTokenUsage | null | undefined>
): GatewayTokenUsage | null {
  let seen = false;
  const total: GatewayTokenUsage = {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    uncached_input_tokens: 0,
    cache_hit_ratio: null,
  };
  for (const item of items) {
    if (!item) continue;
    seen = true;
    const input = inputTokensOf(item);
    const output = outputTokensOf(item);
    total.prompt_tokens += input;
    total.input_tokens = (total.input_tokens || 0) + input;
    total.completion_tokens += output;
    total.output_tokens = (total.output_tokens || 0) + output;
    total.total_tokens += Number(item.total_tokens || 0);
    total.cache_read_tokens =
      (total.cache_read_tokens || 0) + Number(item.cache_read_tokens || 0);
    total.cache_write_tokens =
      (total.cache_write_tokens || 0) + Number(item.cache_write_tokens || 0);
    total.uncached_input_tokens =
      (total.uncached_input_tokens || 0) +
      Number(item.uncached_input_tokens || 0);
  }
  if (!seen) return null;
  const covered =
    (total.cache_read_tokens || 0) + (total.uncached_input_tokens || 0);
  total.cache_hit_ratio =
    covered > 0 ? (total.cache_read_tokens || 0) / covered : null;
  return total;
}

/**
 * The full sentence behind the compact figures.
 *
 * One string for both the tooltip and `title`, so hovering and hearing the
 * cell give the same answer, including the tokens a cached read saved.
 */
export function tokenFiguresTitle(
  usage: GatewayTokenUsage | null | undefined
): string {
  if (!usage) return 'No token usage recorded';
  const input = inputTokensOf(usage);
  const output = outputTokensOf(usage);
  const total = Number(usage.total_tokens || input + output);
  const parts = [
    `${formatExactTokenCount(input)} input tokens`,
    `${formatExactTokenCount(output)} output tokens`,
    `${formatExactTokenCount(total)} total`,
  ];
  const cache = cacheSplitOf(usage);
  if (cache) {
    const write = Number(usage.cache_write_tokens || 0);
    parts.push(
      `${formatExactTokenCount(cache.hit)} input tokens read from cache, ` +
        `${formatExactTokenCount(cache.miss)} not cached ` +
        `(${formatCacheHitRate(cache.ratio)} hit)`
    );
    if (write > 0) {
      parts.push(`${formatExactTokenCount(write)} written to cache`);
    }
  } else {
    parts.push('Cache use not reported for these requests');
  }
  return parts.join('. ');
}

/**
 * Compact token figures for a list cell or a summary strip.
 *
 * Renders "12.4K in - 3.1K out" with an optional cache segment, either
 * "cache 68% hit" (default) or "8.2K hit - 3.9K miss" when `expanded` is set,
 * which is what the cost tables and the usage card want.
 */
@customElement('token-figures')
export class TokenFigures extends LitElement {
  /** The aggregate to state. Null renders the empty marker, not zeroes. */
  @property({ attribute: false })
  usage: GatewayTokenUsage | null = null;

  /** Show the cache split as counts rather than a rate. */
  @property({ type: Boolean })
  expanded = false;

  /** Drop the cache segment entirely, for the narrowest columns. */
  @property({ type: Boolean, attribute: 'hide-cache' })
  hideCache = false;

  /** What to render when there is no usage at all. */
  @property({ type: String })
  empty = '-';

  static styles = css`
    :host {
      display: inline-block;
      font-variant-numeric: tabular-nums;
    }

    .figures {
      display: inline-flex;
      align-items: baseline;
      gap: 6px;
      white-space: nowrap;
    }

    .direction {
      color: var(--sl-color-neutral-900);
    }

    .unit {
      color: var(--sl-color-neutral-500);
      font-size: 0.9em;
    }

    .cache {
      color: var(--sl-color-neutral-500);
      font-size: 0.9em;
    }

    .empty {
      color: var(--sl-color-neutral-500);
    }
  `;

  render() {
    const usage = this.usage;
    const hasUsage =
      !!usage &&
      (Number(usage.total_tokens || 0) > 0 ||
        inputTokensOf(usage) > 0 ||
        outputTokensOf(usage) > 0);
    if (!hasUsage) {
      // No attributable usage is not zero usage, so the cell says nothing
      // rather than claiming a measured zero.
      return html`<span class="empty" title="No token usage recorded"
        >${this.empty}</span
      >`;
    }
    const description = tokenFiguresTitle(usage);
    return html`<sl-tooltip content=${description} hoist>
      <span class="figures" title=${description}>
        <span class="direction"
          >${formatTokenCount(inputTokensOf(usage))}
          <span class="unit">in</span></span
        >
        <span class="unit">·</span>
        <span class="direction"
          >${formatTokenCount(outputTokensOf(usage))}
          <span class="unit">out</span></span
        >
        ${this.renderCache(usage)}
      </span>
    </sl-tooltip>`;
  }

  private renderCache(usage: GatewayTokenUsage) {
    if (this.hideCache) return nothing;
    const cache = cacheSplitOf(usage);
    // Nothing reported means unknown. Saying "0% hit" would invent a
    // measurement the providers never made.
    if (!cache) return nothing;
    if (this.expanded) {
      return html`<span class="cache"
        >${formatTokenCount(cache.hit)} hit · ${formatTokenCount(cache.miss)}
        miss</span
      >`;
    }
    return html`<span class="cache"
      >cache ${formatCacheHitRate(cache.ratio)} hit</span
    >`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'token-figures': TokenFigures;
  }
}
