import { LitElement, html, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { getRepriceJobStatus } from '../api';
import type { RepriceJobStatus } from '../types';
import '@shoelace-style/shoelace/dist/components/button/button.js';

export function formatProviderLookupSummary(
  counts?: Record<string, number> | null
): string {
  if (!counts) return '';
  const count = (key: string) => (counts[key] ?? 0).toLocaleString();
  return `Provider cost lookup: ${count('recovered')} recovered; ${count('missing_id')} missing generation IDs; ${count('unavailable')} unavailable; ${count('deferred')} deferred by limits; ${count('ambiguous_cost')} ambiguous charges.`;
}

/** Bounded observation of a durable job; aggregate usage is not job status. */
@customElement('reprice-job-status')
export class RepriceJobStatusElement extends LitElement {
  static readonly POLL_INTERVAL_MS = 5000;
  static readonly POLL_MAX_ATTEMPTS = 12;

  @property() jobId = '';
  @state() private job: RepriceJobStatus | null = null;
  @state() private checking = false;
  @state() private paused = false;
  @state() private error: string | null = null;
  private generation = 0;

  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('jobId') && this.jobId) {
      this.job = null;
      this.error = null;
      void this.watch();
    }
  }

  connectedCallback(): void {
    super.connectedCallback();
    if (this.hasUpdated && this.jobId && !this.terminal) void this.watch();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.generation++;
  }

  private get terminal(): boolean {
    return this.job?.status === 'succeeded' || this.job?.status === 'failed';
  }

  private async check(generation: number): Promise<void> {
    try {
      const job = await getRepriceJobStatus(this.jobId);
      if (generation !== this.generation || !this.isConnected) return;
      this.job = job;
      this.error = null;
      if (this.terminal) {
        this.dispatchEvent(
          new CustomEvent('reprice-complete', { detail: job })
        );
      }
    } catch (error) {
      if (generation !== this.generation || !this.isConnected) return;
      this.error =
        error instanceof Error
          ? error.message
          : 'Unable to check repricing status';
    }
  }

  private async watch(): Promise<void> {
    const generation = ++this.generation;
    this.checking = true;
    this.paused = false;
    for (
      let attempt = 0;
      attempt < RepriceJobStatusElement.POLL_MAX_ATTEMPTS;
      attempt++
    ) {
      await this.check(generation);
      if (generation !== this.generation || !this.isConnected) return;
      if (this.terminal) break;
      await new Promise((resolve) =>
        window.setTimeout(resolve, RepriceJobStatusElement.POLL_INTERVAL_MS)
      );
      if (generation !== this.generation || !this.isConnected) return;
    }
    this.checking = false;
    this.paused = !this.terminal;
  }

  private async refresh(): Promise<void> {
    this.checking = true;
    await this.check(this.generation);
    this.checking = false;
    this.paused = !this.terminal;
  }

  private get outcome(): string {
    const job = this.job;
    if (!job) return 'Repricing accepted. Checking job status.';
    if (job.stalled && !this.terminal)
      return `Repricing ${job.status}. Worker heartbeat overdue; completion unconfirmed. Check worker availability.`;
    if (job.status === 'queued')
      return 'Repricing queued. Waiting for a worker.';
    if (job.status === 'running') return 'Repricing running.';
    if (job.status === 'failed')
      return `Repricing failed. ${job.error || 'The worker could not finish this job.'}`;
    if (job.rows_examined === null || job.rows_updated === null)
      return job.dry_run
        ? 'Repricing preview succeeded. Row counts are unavailable.'
        : 'Repricing succeeded. Row counts are unavailable.';
    return `${job.dry_run ? 'Repricing preview succeeded' : 'Repricing succeeded'}: ${job.rows_updated.toLocaleString()} of ${job.rows_examined.toLocaleString()} requests ${job.dry_run ? 'would be updated' : 'updated'}${job.rows_skipped === null ? '' : `, ${job.rows_skipped.toLocaleString()} skipped`}.`;
  }

  render() {
    return html`
      <div role=${this.job?.status === 'failed' ? 'alert' : 'status'}>
        <div>${this.outcome}</div>
        ${this.job?.provider_lookup ? html`<div>${formatProviderLookupSummary(this.job.provider_lookup)}</div>` : nothing}
        <div>Job ID: <code>${this.jobId}</code></div>
        ${(this.job?.attempts ?? 0) > 1 ? html`<div>Attempt ${this.job!.attempts}; counts are for this attempt. Earlier attempts may have updated some requests.</div>` : nothing}
        ${this.error ? html`<div>${this.error}. Completion is unconfirmed.</div>` : nothing}
        ${this.paused ? html`<div>Automatic checks stopped. Completion is unconfirmed. Check this job again for an update.</div>` : nothing}
        ${
          !this.terminal
            ? html`<sl-button
                size="small"
                ?loading=${this.checking}
                ?disabled=${this.checking}
                @click=${() => void this.refresh()}
                >Check status</sl-button
              >`
            : nothing
        }
      </div>
    `;
  }
}
