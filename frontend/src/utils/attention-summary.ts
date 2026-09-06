/**
 * What the console header knows about the attention items, without asking.
 *
 * The bell used to say "No notifications" while the amber strip one line
 * below it said "2 need attention", which reads as a contradiction even
 * though both are true (there are no notification rows, and there are two
 * attention items). The header cannot afford the nine requests the attention
 * rules need on every page, so whoever already derived the items publishes
 * the counts here: the Overview strip and the Attention page. The header
 * reads the last published summary and states it in the empty state.
 *
 * The summary lives in sessionStorage so it survives a route change inside
 * the tab, and an event carries it to any header already on screen.
 */
import {
  ATTENTION_KIND_ORDER,
  attentionKindChipLabel,
  type AttentionItem,
  type AttentionKind,
} from './attention';

export interface AttentionSummary {
  /** How many items the strip would show. */
  total: number;
  /** Per kind, in the console's kind order; kinds with no item are absent. */
  counts: Array<{ kind: AttentionKind; count: number }>;
  /**
   * True when every item is low severity (a model priced at $0): those are a
   * question, not a problem, and the strip words them "worth a look".
   */
  lowOnly: boolean;
}

export const ATTENTION_SUMMARY_EVENT = 'preloop-attention-summary';
const STORAGE_KEY = 'preloop:attention-summary';

/** The same slice the Overview strip shows: loud items, or the low ones alone. */
export function summariseAttentionItems(
  items: AttentionItem[]
): AttentionSummary {
  const loud = items.filter((item) => item.severity !== 'low');
  const lowOnly = loud.length === 0;
  const shown = lowOnly ? items : loud;
  const counts = ATTENTION_KIND_ORDER.map((kind) => ({
    kind,
    count: shown.filter((item) => item.kind === kind).length,
  })).filter((entry) => entry.count > 0);
  return { total: shown.length, counts, lowOnly };
}

export function publishAttentionSummary(items: AttentionItem[]): void {
  const summary = summariseAttentionItems(items);
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(summary));
  } catch {
    // A full or blocked sessionStorage costs the header its memory across
    // route changes, nothing else.
  }
  window.dispatchEvent(
    new CustomEvent<AttentionSummary>(ATTENTION_SUMMARY_EVENT, {
      detail: summary,
    })
  );
}

export function readAttentionSummary(): AttentionSummary | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AttentionSummary;
    if (typeof parsed?.total !== 'number' || !Array.isArray(parsed.counts)) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

/**
 * "2 items need attention: 1 flow, 1 pricing", in the strip's own words.
 * Empty when there is nothing to say, so the caller can render nothing.
 */
export function formatAttentionSummary(
  summary: AttentionSummary | null
): string {
  if (!summary || summary.total < 1) return '';
  const noun = summary.total === 1 ? 'item' : 'items';
  const verb = summary.lowOnly
    ? 'worth a look'
    : summary.total === 1
      ? 'needs attention'
      : 'need attention';
  const parts = summary.counts.map((entry) =>
    attentionKindChipLabel(entry.kind, entry.count)
  );
  const detail = parts.length ? `: ${parts.join(', ')}` : '';
  return `${summary.total} ${noun} ${verb}${detail}`;
}
