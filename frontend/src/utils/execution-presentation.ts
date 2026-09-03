import { html, TemplateResult } from 'lit';

/**
 * How an execution presents itself in the console: its status chip, its cost,
 * and which model ran it.
 *
 * The executions list and the execution page show the same three facts, so
 * they read them the same way here rather than each inventing a label, a
 * rounding rule and a "+1 more" affordance of its own.
 */

/** One model alias an execution used, with how many requests went to it. */
export interface ExecutionModelUsage {
  model_alias: string;
  provider_name?: string | null;
  request_count?: number;
}

/** The model projection both execution endpoints carry (wave 7). */
export interface ExecutionModelSource {
  model_alias?: string | null;
  provider_name?: string | null;
  models_used?: ExecutionModelUsage[] | null;
}

/**
 * Status as a word rather than a shout: SUCCEEDED is a database value,
 * "Succeeded" is what a person reads.
 */
export function executionStatusLabel(
  status: string | null | undefined
): string {
  const raw = (status || '').trim();
  if (!raw) return 'Unknown';
  const words = raw.toLowerCase().split(/[_\s]+/);
  return words
    .map((word, index) =>
      index === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word
    )
    .join(' ');
}

/**
 * One taxonomy for every execution chip: green finished, red failed, blue
 * still going, neutral for the rest (pending, cancelled, stopped). Red is
 * reserved for a run that actually broke.
 */
export function executionStatusVariant(
  status: string | null | undefined
): 'success' | 'danger' | 'primary' | 'neutral' {
  switch ((status || '').toUpperCase()) {
    case 'SUCCEEDED':
      return 'success';
    case 'FAILED':
    case 'TIMEOUT':
      return 'danger';
    case 'RUNNING':
    case 'STARTING':
    case 'INITIALIZING':
      return 'primary';
    default:
      return 'neutral';
  }
}

/**
 * Estimated spend for a run.
 *
 * An unpriced model reports 0, which is not "free": it is "we could not
 * price this", and a dash says that without claiming a number. Sub-cent
 * spend rounds to "<$0.01" rather than to "$0.00".
 */
export function formatEstimatedCost(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value) || value <= 0) {
    return '—';
  }
  if (value < 0.01) return '<$0.01';
  return `$${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Every alias a run used, primary first, from either projected shape. */
export function executionModels(
  execution: ExecutionModelSource
): ExecutionModelUsage[] {
  const models = Array.isArray(execution.models_used)
    ? execution.models_used.filter((entry) => entry && entry.model_alias)
    : [];
  if (models.length > 0) return models;
  if (execution.model_alias) {
    return [
      {
        model_alias: execution.model_alias,
        provider_name: execution.provider_name,
      },
    ];
  }
  return [];
}

/** Tooltip text listing every alias with its share of the requests. */
export function executionModelTitle(execution: ExecutionModelSource): string {
  const models = executionModels(execution);
  if (models.length === 0) return 'No gateway usage recorded for this run';
  return models
    .map((model) => {
      const provider = model.provider_name ? ` (${model.provider_name})` : '';
      const count =
        typeof model.request_count === 'number' && model.request_count > 0
          ? `: ${model.request_count.toLocaleString()} request${
              model.request_count === 1 ? '' : 's'
            }`
          : '';
      return `${model.model_alias}${provider}${count}`;
    })
    .join('\n');
}

/**
 * The model cell: the alias that did most of the work, its provider in meta
 * ink, and "+N" when the run switched models. The full list is in the title,
 * because a table row is not the place to enumerate four aliases.
 */
export function renderExecutionModel(
  execution: ExecutionModelSource
): TemplateResult {
  const models = executionModels(execution);
  if (models.length === 0) {
    return html`<span
      class="execution-model is-empty"
      title="No gateway usage recorded for this run"
      >—</span
    >`;
  }
  const [primary, ...rest] = models;
  return html`<span
    class="execution-model"
    title=${executionModelTitle(execution)}
    ><span class="execution-model-alias">${primary.model_alias}</span>${
      primary.provider_name
        ? html`<span class="execution-model-provider"
            >${primary.provider_name}</span
          >`
        : ''
    }${
      rest.length
        ? html`<span class="execution-model-more">+${rest.length}</span>`
        : ''
    }</span
  >`;
}

/** Shared styling for the cell above, as a plain CSS string. */
export const executionModelCss = `
  .execution-model {
    align-items: baseline;
    display: inline-flex;
    gap: 6px;
    max-width: 100%;
    min-width: 0;
  }
  .execution-model-alias {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The provider qualifies the alias, so it never competes with it. */
  .execution-model-provider,
  .execution-model-more,
  .execution-model.is-empty {
    color: var(--console-meta-color);
    font-size: var(--console-text-meta);
    white-space: nowrap;
  }
`;
