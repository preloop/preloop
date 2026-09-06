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

/**
 * A token count at console scale.
 *
 * `1,353,363` is a number to compare, not to read digit by digit, so at or
 * above 1000 it renders compact ("1.4M"). The exact figure belongs in a
 * title, not in the strip.
 */
export function formatTokenCount(value: number | null | undefined): string {
  const amount = Number(value ?? 0);
  if (!Number.isFinite(amount) || amount <= 0) return '0';
  if (amount < 1000) return String(Math.round(amount));
  return new Intl.NumberFormat(undefined, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount);
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
 * The alias as a column prints it: without the provider prefix the cell (or
 * the next column) already states. `deepseek/deepseek-v4-pro` beside a
 * provider that says `deepseek` reads as the same word twice (DESIGN.md D30).
 */
export function executionModelAliasLabel(model: ExecutionModelUsage): string {
  const alias = (model.model_alias || '').trim();
  const provider = (model.provider_name || '').trim().toLowerCase();
  if (!provider || !alias.includes('/')) return alias;
  const [prefix, ...rest] = alias.split('/');
  const remainder = rest.join('/');
  return prefix.toLowerCase() === provider && remainder ? remainder : alias;
}

/**
 * The model cell: the alias that did most of the work, its provider in meta
 * ink, and "+N" when the run switched models. The full list is in the title,
 * because a table row is not the place to enumerate four aliases.
 *
 * `aliasOnly` is for tables with no provider column (the executions list):
 * the alias drops a provider prefix it would otherwise print twice, and the
 * provider itself stays in the title.
 */
export function renderExecutionModel(
  execution: ExecutionModelSource,
  options: { aliasOnly?: boolean } = {}
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
  const alias = options.aliasOnly
    ? executionModelAliasLabel(primary)
    : primary.model_alias;
  return html`<span
    class="execution-model"
    title=${executionModelTitle(execution)}
    ><span class="execution-model-alias">${alias}</span>${
      primary.provider_name && !options.aliasOnly
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

/** Where a run executed, as both execution endpoints project it. */
export interface ExecutionRunner {
  kind: 'private' | 'hosted';
  id?: string | null;
  name: string;
  pool?: string | null;
}

/** Hosted is the default so an older payload still names the executor. */
export function executionRunner(
  runner?: ExecutionRunner | null
): ExecutionRunner {
  if (runner && (runner.kind === 'private' || runner.kind === 'hosted')) {
    const fallback =
      runner.kind === 'hosted' ? 'Preloop hosted' : 'Private runner';
    return {
      kind: runner.kind,
      id: runner.id ?? null,
      name: runner.name || fallback,
      pool: runner.pool ?? null,
    };
  }
  return { kind: 'hosted', id: null, name: 'Preloop hosted', pool: null };
}

/**
 * Whether a row has to say where it ran.
 *
 * Repeating "Hosted" on all 25 rows states the account default 25 times. The
 * chip earns its place only on a run that went somewhere the default would
 * not have sent it: with a pinned private pool that is a hosted run, and with
 * auto or hosted-only (where hosted is the fallback) it is a private one.
 */
export function shouldShowRunnerKind(
  runner: ExecutionRunner | null | undefined,
  accountDefaultPool?: string | null
): boolean {
  const kind = executionRunner(runner).kind;
  const pool = (accountDefaultPool || '').trim().toLowerCase();
  const defaultsToPrivate = pool !== '' && pool !== 'auto' && pool !== 'server';
  return defaultsToPrivate ? kind === 'hosted' : kind === 'private';
}

/** Small Hosted / Private chip used on the list and the run page. */
export function renderExecutionRunnerKind(
  runner?: ExecutionRunner | null
): TemplateResult {
  const resolved = executionRunner(runner);
  const label = resolved.kind === 'private' ? 'Private' : 'Hosted';
  return html`<sl-badge
    class="chip runner-kind-badge"
    pill
    variant="neutral"
    data-testid="runner-kind-badge"
    data-runner-kind=${resolved.kind}
    >${label}</sl-badge
  >`;
}

/**
 * The "Ran on" value: runner name, kind chip, pool when set. Private names
 * link to the Runners settings page.
 */
export function renderExecutionRunner(
  runner?: ExecutionRunner | null
): TemplateResult {
  const resolved = executionRunner(runner);
  const name =
    resolved.kind === 'private'
      ? html`<a
          class="execution-runner-name strip-link"
          href="/console/settings/runners"
          >${resolved.name}</a
        >`
      : html`<span class="execution-runner-name">${resolved.name}</span>`;
  return html`<span class="execution-runner" data-testid="execution-runner"
    >${name}${renderExecutionRunnerKind(resolved)}${
      resolved.pool
        ? html`<span class="execution-runner-pool">${resolved.pool}</span>`
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
  .execution-runner {
    align-items: baseline;
    display: inline-flex;
    flex-wrap: wrap;
    gap: 6px;
    max-width: 100%;
    min-width: 0;
  }
  .execution-runner-name {
    min-width: 0;
  }
  .execution-runner-pool,
  .runner-kind-badge {
    color: var(--console-meta-color);
    font-size: var(--console-text-meta);
    white-space: nowrap;
  }
`;
