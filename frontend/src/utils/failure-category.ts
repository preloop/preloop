import { html, nothing, TemplateResult } from 'lit';

/**
 * Why a flow run failed, in one word the console can group by.
 *
 * The backend derives `failure_category` once, at failure time, from the
 * raising code, the shape of the message and the executor's own analysis of
 * the logs (issue #361), and stores one of a closed vocabulary. The console
 * never re-derives it from `error_message`: a heuristic in the browser and a
 * heuristic on the server would disagree, and only one of them is stored.
 *
 * Every surface that lists a failed run shows the same chip from here, so
 * "runner conflict" means the same thing on the executions table, the run
 * page, the attention evidence and the feed.
 */

interface FailureCategoryMeta {
  /** Lower case, for a sentence: "3 model transient, 2 no confirmation". */
  label: string;
  /** One line saying what actually happened, shown as the chip's tooltip. */
  tooltip: string;
}

/**
 * The vocabulary as the server writes it. Order is the order a summary lists
 * them in when counts tie: infrastructure first, then the model, then the
 * agent's own work, then the two states that are nobody's fault.
 */
export const FAILURE_CATEGORY_META: Record<string, FailureCategoryMeta> = {
  runner_conflict: {
    label: 'runner conflict',
    tooltip:
      'The runner could not start the agent because a job of the same name was still there.',
  },
  runner_error: {
    label: 'runner error',
    tooltip:
      'The runner could not start the agent at all, so nothing of this run ran.',
  },
  model_transient: {
    label: 'model transient',
    tooltip:
      'The provider dropped the stream or throttled the call; the same run usually works on a retry.',
  },
  model_auth: {
    label: 'model auth',
    tooltip:
      'The gateway rejected the credentials this run was using, so the model was never reached.',
  },
  provider_billing: {
    label: 'provider billing',
    tooltip:
      'The provider refused the call: billing or quota. Retry after the account is topped up.',
  },
  /**
   * Superseded by `provider_billing`, which the server writes now. Kept so
   * runs classified before it still read as something.
   */
  model_quota: {
    label: 'model quota',
    tooltip: 'The provider refused the call on quota or spending limits.',
  },
  model_config: {
    label: 'model config',
    tooltip:
      'The provider rejected the shape of the request, such as a parameter this model does not support.',
  },
  no_confirmation: {
    label: 'no confirmation',
    tooltip:
      'The agent exited cleanly but never signalled that it had finished, so Preloop cannot say the work is done.',
  },
  tool_error: {
    label: 'tool error',
    tooltip: 'A command the agent ran in its own workspace exited non-zero.',
  },
  agent_error: {
    label: 'agent error',
    tooltip: 'The agent process itself ended with an error.',
  },
  timeout: {
    label: 'timeout',
    tooltip: 'The run reached its time limit and was stopped.',
  },
  cancelled: {
    label: 'cancelled',
    tooltip: 'Somebody stopped this run before it finished.',
  },
  unknown: {
    label: 'unknown',
    tooltip:
      'Preloop could not place this failure from what the run recorded. A rising share of these is a gap in the vocabulary.',
  },
};

/** The order a breakdown lists equal counts in. */
export const FAILURE_CATEGORY_ORDER: string[] = Object.keys(
  FAILURE_CATEGORY_META
);

function normalize(value: string | null | undefined): string {
  return (value || '').trim().toLowerCase();
}

/**
 * The category as a phrase. A value the console has never seen is humanised
 * rather than dropped: the vocabulary grows on the server, and a console that
 * silently hides what it does not recognise is a console that stops being a
 * record (same rule as unknown audit event types).
 */
export function failureCategoryLabel(value: string | null | undefined): string {
  const key = normalize(value);
  if (!key) return '';
  const known = FAILURE_CATEGORY_META[key];
  if (known) return known.label;
  return key.replace(/[_\s]+/g, ' ');
}

/** Sentence case, for a chip that sits beside "Failed". */
export function failureCategoryChipLabel(
  value: string | null | undefined
): string {
  const label = failureCategoryLabel(value);
  if (!label) return '';
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/**
 * The `model_transient` line without its retry promise.
 *
 * Servers older than `provider_billing` filed HTTP 402 "Insufficient
 * Balance" under `model_transient`, whose tooltip says the run "usually works
 * on a retry". When the page it is shown on holds the gateway calls, and
 * those calls came back 4xx, the console can see that the promise is false,
 * so it stops making it rather than contradicting the evidence beside it.
 */
const MODEL_TRANSIENT_NO_RETRY_TOOLTIP =
  'The provider dropped the stream or refused the call. The gateway calls on this run came back 4xx, so a retry may fail the same way.';

/** The one line under the pointer. Unknown values say only what they are. */
export function failureCategoryTooltip(
  value: string | null | undefined,
  options: { retryDoubtful?: boolean } = {}
): string {
  const key = normalize(value);
  if (!key) return '';
  if (key === 'model_transient' && options.retryDoubtful) {
    return MODEL_TRANSIENT_NO_RETRY_TOOLTIP;
  }
  const known = FAILURE_CATEGORY_META[key];
  if (known) return known.tooltip;
  return `This run failed with the category ${failureCategoryLabel(
    key
  )}, which this console does not have a description for yet.`;
}

/**
 * The chip itself: the soft neutral recipe every other state chip uses, never
 * a colour of its own. Red already lives in the status pill this sits after,
 * and a second red object beside it would state the same failure twice.
 *
 * Returns `nothing` when the field is absent, which is what a server older
 * than #361 sends and what every non-failed run carries.
 */
export function renderFailureCategoryChip(
  value: string | null | undefined,
  options: { tooltip?: boolean; retryDoubtful?: boolean } = {}
): TemplateResult | typeof nothing {
  const key = normalize(value);
  if (!key) return nothing;
  const chip = html`<sl-badge
    class="chip failure-category-chip"
    pill
    variant="neutral"
    data-failure-category=${key}
    >${failureCategoryChipLabel(key)}</sl-badge
  >`;
  if (options.tooltip === false) {
    return chip;
  }
  return html`<sl-tooltip
    content=${failureCategoryTooltip(key, {
      retryDoubtful: options.retryDoubtful,
    })}
    hoist
    >${chip}</sl-tooltip
  >`;
}

/** How many runs failed each way, most common first. */
export function failureCategoryCounts(
  values: Array<string | null | undefined>
): Array<{ category: string; count: number }> {
  const counts = new Map<string, number>();
  for (const value of values) {
    const key = normalize(value);
    if (!key) continue;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return Array.from(counts.entries())
    .map(([category, count]) => ({ category, count }))
    .sort((left, right) => {
      if (right.count !== left.count) return right.count - left.count;
      const leftRank = FAILURE_CATEGORY_ORDER.indexOf(left.category);
      const rightRank = FAILURE_CATEGORY_ORDER.indexOf(right.category);
      return (
        (leftRank === -1 ? Number.MAX_SAFE_INTEGER : leftRank) -
        (rightRank === -1 ? Number.MAX_SAFE_INTEGER : rightRank)
      );
    });
}

/**
 * "3 model transient, 2 no confirmation" - what a count of failures is
 * actually made of. Empty when nothing carries a category, so the sentence
 * that uses it degrades to the plain count.
 */
export function failureCategoryBreakdown(
  values: Array<string | null | undefined>
): string {
  return failureCategoryCounts(values)
    .map(({ category, count }) => `${count} ${failureCategoryLabel(category)}`)
    .join(', ');
}
