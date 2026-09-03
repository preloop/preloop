import { css, html, nothing, type TemplateResult } from 'lit';

/**
 * The spend meter shared by `budget-health-card` (Cost) and `usage-card`
 * (Overview): a track that fills green up to the soft limit, amber between
 * soft and hard, red once a limit is reached, with a marker at the soft limit
 * and one at the hard end.
 */
export const budgetTrackStyles = css`
  .budget-track {
    position: relative;
    height: 6px;
    border-radius: 999px;
    background: var(--sl-color-neutral-200);
    overflow: hidden;
  }

  .budget-track-fill {
    position: absolute;
    top: 0;
    bottom: 0;
    left: var(--budget-fill-left, 0%);
    width: var(--budget-fill-width, 0%);
    background: var(--sl-color-success-600);
  }

  .budget-track-fill.warning {
    background: var(--sl-color-warning-600);
  }

  .budget-track-fill.danger {
    background: var(--sl-color-danger-600);
  }

  .budget-soft-marker {
    position: absolute;
    top: 0;
    bottom: 0;
    left: var(--budget-soft-position, 0%);
    width: 2px;
    background: var(--sl-color-warning-600);
    box-shadow: 0 0 0 1px var(--console-surface);
  }

  .budget-hard-marker {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    width: 2px;
    background: var(--sl-color-danger-600);
    box-shadow: 0 0 0 1px var(--console-surface);
  }
`;

export function isHardLimitExceeded(spend: number, hardLimit: number): boolean {
  return hardLimit > 0 && spend >= hardLimit;
}

export function isSoftOnlyLimitExceeded(
  spend: number,
  softLimit: number,
  hardLimit: number
): boolean {
  return softLimit > 0 && hardLimit <= 0 && spend >= softLimit;
}

export function isLimitExceeded(
  spend: number,
  softLimit: number,
  hardLimit: number
): boolean {
  return (
    isHardLimitExceeded(spend, hardLimit) ||
    isSoftOnlyLimitExceeded(spend, softLimit, hardLimit)
  );
}

export function isSoftLimitWarning(
  spend: number,
  softLimit: number,
  hardLimit: number
): boolean {
  return (
    softLimit > 0 && hardLimit > 0 && spend >= softLimit && spend < hardLimit
  );
}

export type BudgetTone = 'success' | 'warning' | 'danger';

export function budgetTone(
  spend: number,
  softLimit: number,
  hardLimit: number
): BudgetTone {
  if (isLimitExceeded(spend, softLimit, hardLimit)) return 'danger';
  if (isSoftLimitWarning(spend, softLimit, hardLimit)) return 'warning';
  return 'success';
}

function formatCurrency(value: number): string {
  const amount = Number(value || 0);
  if (amount > 0 && amount < 0.01) return `$${amount.toFixed(4)}`;
  return `$${amount.toFixed(2)}`;
}

export interface BudgetTrackOptions {
  spend: number;
  softLimit: number;
  hardLimit: number;
  label: string;
}

/** Renders the meter itself; callers own the surrounding label row. */
export function renderBudgetTrack({
  spend,
  softLimit,
  hardLimit,
  label,
}: BudgetTrackOptions): TemplateResult | typeof nothing {
  const maxLimit = hardLimit || softLimit;
  if (maxLimit <= 0) return nothing;

  const fillPercent = Math.min(100, (spend / maxLimit) * 100);
  const softPercent =
    softLimit > 0 ? Math.min(100, (softLimit / maxLimit) * 100) : 0;
  const limitExceeded = isLimitExceeded(spend, softLimit, hardLimit);
  const softLimitWarning = isSoftLimitWarning(spend, softLimit, hardLimit);
  const successFillPercent = limitExceeded
    ? 0
    : softLimit > 0
      ? Math.min(fillPercent, softPercent)
      : fillPercent;
  const warningFillPercent =
    !limitExceeded && softLimitWarning ? fillPercent - softPercent : 0;
  const dangerFillPercent = limitExceeded ? fillPercent : 0;

  return html`
    <div
      class="budget-track"
      role="progressbar"
      aria-label=${`${label} usage`}
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow=${Math.round(fillPercent)}
    >
      ${
        successFillPercent > 0
          ? html`<div
              class="budget-track-fill"
              style="--budget-fill-width: ${successFillPercent}%;"
            ></div>`
          : nothing
      }
      ${
        warningFillPercent > 0
          ? html`<div
              class="budget-track-fill warning"
              style="--budget-fill-left: ${softPercent}%; --budget-fill-width: ${warningFillPercent}%;"
            ></div>`
          : nothing
      }
      ${
        dangerFillPercent > 0
          ? html`<div
              class="budget-track-fill danger"
              style="--budget-fill-width: ${dangerFillPercent}%;"
            ></div>`
          : nothing
      }
      ${
        softLimit > 0 && hardLimit > 0 && softLimit < hardLimit
          ? html`<div
              class="budget-soft-marker"
              title=${`Soft limit ${formatCurrency(softLimit)}`}
              style="--budget-soft-position: ${softPercent}%;"
            ></div>`
          : nothing
      }
      ${
        hardLimit > 0
          ? html`<div
              class="budget-hard-marker"
              title=${`Hard limit ${formatCurrency(hardLimit)}`}
            ></div>`
          : nothing
      }
    </div>
  `;
}
