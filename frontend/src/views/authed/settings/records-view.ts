import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '../../../components/view-header.ts';
import { confirmDialog, showToast } from '../../../components/confirm-dialog';
import { getUserProfile, hasPermission } from '../../../api';
import {
  createLegalHold,
  createPeriodExport,
  getAuditChainStatus,
  getRetentionSettings,
  listAuditChainCheckpoints,
  listLegalHolds,
  listSigningKeys,
  previewRetentionPurge,
  releaseLegalHold,
  rotateSigningKey,
  updateRetentionSettings,
  verifyAuditChain,
  type ChainCheckpoint,
  type ChainStatus,
  type ChainVerifyResult,
  type LegalHold,
  type PurgePreview,
  type RetentionSettings,
  type SigningKeyList,
} from '../../../records-api';
import { consoleDialogStyles } from '../../../styles/console-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';
import {
  CHAIN_HONESTY,
  DEFAULT_VERIFY_ROWS,
  HOLD_DOES,
  HOLD_DOES_NOT,
  MAX_EXPORT_DAYS,
  MAX_HOLD_REASON,
  MIN_HOLD_REASON,
  OBJECT_LOCK_NOTE,
  clampRetentionDays,
  defaultVerifyRange,
  downloadBlob,
  downloadText,
  evidenceVerifyCommand,
  holdResourceHref,
  isUuid,
  offlineAuditCommand,
  periodDefaultRange,
  retentionDiff,
  retentionRowEditable,
  retentionUpdatePayload,
  truncateMiddle,
  wholeChainRange,
  type VerifyRange,
} from '../../../utils/records-format';

const CHECKPOINT_PAGE = 50;
const RESOURCE_TYPES = [
  { value: 'execution', label: 'Flow execution' },
  { value: 'approval', label: 'Approval request' },
  { value: 'evidence_pack', label: 'Evidence pack' },
  { value: 'runtime_session', label: 'Runtime session' },
] as const;

@customElement('records-view')
export class RecordsView extends LitElement {
  @state() private canAudit = false;
  @state() private canPolicies = false;
  @state() private canManage = false;
  @state() private permissionsReady = false;

  @state() private status: ChainStatus | null = null;
  @state() private statusError: string | null = null;
  @state() private verifyMode: 'recent' | 'whole' | 'custom' = 'recent';
  @state() private rangeStart = '';
  @state() private rangeEnd = '';
  @state() private verifying = false;
  @state() private verdict: ChainVerifyResult | null = null;
  @state() private verifyError: string | null = null;

  @state() private checkpoints: ChainCheckpoint[] = [];
  @state() private checkpointCursor = 0;
  @state() private checkpointsDone = false;

  @state() private keys: SigningKeyList | null = null;
  @state() private keysError: string | null = null;
  @state() private rotating = false;

  @state() private retention: RetentionSettings | null = null;
  @state() private drafts: Record<string, number> = {};
  @state() private retentionError: string | null = null;
  @state() private savingRetention = false;
  @state() private preview: PurgePreview | null = null;
  @state() private previewing = false;

  @state() private holds: LegalHold[] = [];
  @state() private holdsError: string | null = null;
  @state() private showReleased = false;
  @state() private placeOpen = false;
  @state() private placeType = 'execution';
  @state() private placeId = '';
  @state() private placeReason = '';
  @state() private placeError: string | null = null;
  @state() private placing = false;
  @state() private picker: { id: string; label: string }[] = [];
  @state() private releaseTarget: LegalHold | null = null;
  @state() private releaseReason = '';
  @state() private releaseError: string | null = null;
  @state() private releasing = false;

  @state() private exportStart = '';
  @state() private exportEnd = '';
  @state() private exporting = false;
  @state() private exportError: string | null = null;
  @state() private exportResult: {
    filename: string;
    sizeBytes: number;
    keyId: string | null;
    signature: string | null;
  } | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      .jump {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        margin-bottom: var(--sl-spacing-large);
      }
      .section {
        scroll-margin-top: 72px;
        margin-bottom: var(--sl-spacing-large);
      }
      .facts {
        display: grid;
        grid-template-columns: minmax(9rem, 16rem) 1fr;
      }
      .facts dt,
      .facts dd {
        margin: 0;
        padding: var(--sl-spacing-x-small) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .facts dt {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
      }
      .honesty,
      .note {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
      }
      .command {
        background: var(--console-page);
        padding: var(--sl-spacing-small);
        overflow-x: auto;
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
      }
      .row-actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: end;
        margin-top: var(--sl-spacing-medium);
      }
      .danger-gap {
        margin-left: var(--sl-spacing-large);
      }
      .warn {
        color: var(--sl-color-warning-700);
      }
      @media (max-width: 640px) {
        .facts {
          grid-template-columns: 1fr;
        }
        .danger-gap {
          margin-left: 0;
        }
        .row-actions {
          flex-direction: column;
          align-items: stretch;
        }
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    const period = periodDefaultRange();
    this.exportStart = period.start;
    this.exportEnd = period.end;
    const params = new URLSearchParams(window.location.search);
    if (params.get('start_seq')) {
      this.verifyMode = 'custom';
      this.rangeStart = params.get('start_seq') || '';
      this.rangeEnd = params.get('end_seq') || '';
    }
    void this.load();
  }

  protected firstUpdated(): void {
    const id = window.location.hash.replace(/^#/, '');
    if (id) {
      this.jump(id);
    }
  }

  private onJump = (event: Event): void => {
    const anchor = event.currentTarget as HTMLAnchorElement;
    const id = (anchor.getAttribute('href') || '').replace(/^#/, '');
    event.preventDefault();
    if (!id) {
      return;
    }
    window.history.replaceState(null, '', `#${id}`);
    this.jump(id);
  };

  private jump(id: string): void {
    this.shadowRoot?.getElementById(id)?.scrollIntoView({ block: 'start' });
  }

  private async load(): Promise<void> {
    try {
      const profile = await getUserProfile();
      this.canAudit = hasPermission(profile.permissions, 'view_audit_logs');
      this.canPolicies = hasPermission(profile.permissions, 'view_policies');
      this.canManage = hasPermission(profile.permissions, 'manage_policies');
    } catch {
      this.canAudit = false;
      this.canPolicies = false;
      this.canManage = false;
    }
    this.permissionsReady = true;
    if (this.canAudit) {
      void this.loadStatus();
      void this.loadCheckpoints(true);
      void this.loadKeys();
    }
    if (this.canPolicies) {
      void this.loadRetention();
      void this.loadHolds();
    }
  }

  private async loadStatus(): Promise<void> {
    try {
      this.status = await getAuditChainStatus();
      this.statusError = null;
      if (this.verifyMode !== 'custom' && this.status) {
        const range =
          this.verifyMode === 'whole'
            ? wholeChainRange(this.status)
            : defaultVerifyRange(this.status);
        this.rangeStart = String(range.start);
        this.rangeEnd = String(range.end);
      }
    } catch (err) {
      this.statusError =
        err instanceof Error ? err.message : 'Could not load audit integrity';
    }
  }

  private async loadCheckpoints(reset: boolean): Promise<void> {
    const after = reset ? 0 : this.checkpointCursor;
    try {
      const page = await listAuditChainCheckpoints({
        afterSeq: after,
        limit: CHECKPOINT_PAGE,
      });
      const rows = Array.isArray(page) ? page : [];
      this.checkpoints = reset ? rows : this.checkpoints.concat(rows);
      this.checkpointsDone = rows.length < CHECKPOINT_PAGE;
      const last = rows[rows.length - 1];
      if (last) {
        this.checkpointCursor = last.seq;
      }
    } catch (err) {
      if (reset) {
        this.statusError =
          err instanceof Error ? err.message : 'Could not load checkpoints';
      }
    }
  }

  private async loadKeys(): Promise<void> {
    try {
      const keys = await listSigningKeys();
      this.keys = keys && Array.isArray(keys.keys) ? keys : null;
      this.keysError = this.keys ? null : 'Signing keys were not returned.';
    } catch (err) {
      this.keysError =
        err instanceof Error ? err.message : 'Could not load signing keys';
    }
  }

  private async loadRetention(): Promise<void> {
    try {
      const settings = await getRetentionSettings();
      if (!settings || !Array.isArray(settings.classes)) {
        this.retentionError = 'Retention settings were not returned.';
        return;
      }
      this.retention = settings;
      this.drafts = Object.fromEntries(
        settings.classes.map((row) => [row.record_class, row.days])
      );
      this.retentionError = null;
    } catch (err) {
      this.retentionError =
        err instanceof Error ? err.message : 'Could not load retention';
    }
  }

  private async loadHolds(): Promise<void> {
    try {
      const rows = await listLegalHolds({
        activeOnly: !this.showReleased,
        limit: 200,
      });
      this.holds = Array.isArray(rows) ? rows : [];
      this.holdsError = null;
    } catch (err) {
      this.holdsError =
        err instanceof Error ? err.message : 'Could not load legal holds';
    }
  }

  private selectedRange(): VerifyRange | null {
    if (!this.status) {
      return null;
    }
    if (this.verifyMode === 'whole') {
      return wholeChainRange(this.status);
    }
    if (this.verifyMode === 'recent') {
      return defaultVerifyRange(this.status);
    }
    const start = Number(this.rangeStart);
    const end = Number(this.rangeEnd);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1) {
      return null;
    }
    return { start, end };
  }

  private async verify(): Promise<void> {
    const range = this.selectedRange();
    if (!range || range.end < range.start || this.status?.head_seq === 0) {
      this.verifyError = 'Nothing is sealed in this range.';
      return;
    }
    this.verifying = true;
    this.verifyError = null;
    try {
      this.verdict = await verifyAuditChain({
        startSeq: range.start,
        endSeq: range.end,
      });
    } catch (err) {
      this.verdict = null;
      this.verifyError =
        err instanceof Error ? err.message : 'Verification failed';
    } finally {
      this.verifying = false;
    }
  }

  private async rotate(): Promise<void> {
    const ok = await confirmDialog({
      title: 'Rotate signing key',
      message:
        'The current key is retired and stays published. Signatures it already made stay valid. New signatures use the replacement key.',
      detail:
        'Invalidating old signatures would revoke records you already exported.',
      confirmLabel: 'Rotate key',
      variant: 'danger',
    });
    if (!ok) {
      return;
    }
    this.rotating = true;
    try {
      await rotateSigningKey();
      showToast('Signing key rotated. Retired keys stay published.', 'success');
      await this.loadKeys();
      await this.loadStatus();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Could not rotate the key',
        'danger'
      );
    } finally {
      this.rotating = false;
    }
  }

  private setDraft(recordClass: string, raw: string): void {
    if (!this.retention) {
      return;
    }
    const parsed = Number(raw);
    this.drafts = {
      ...this.drafts,
      [recordClass]: clampRetentionDays(
        parsed,
        this.retention.floor_days,
        this.retention.max_days
      ),
    };
  }

  private async saveRetention(): Promise<void> {
    if (!this.retention) {
      return;
    }
    const diff = retentionDiff(this.retention.classes, this.drafts);
    if (diff.length === 0) {
      return;
    }
    const lines = diff
      .map((row) => `${row.label}: ${row.from} days to ${row.to} days`)
      .join('\n');
    const ok = await confirmDialog({
      title: 'Save retention',
      message:
        'These day counts become the account policy. A class left out of the save returns to the deployment default.',
      detail: lines,
      confirmLabel: 'Save retention',
      variant: 'danger',
    });
    if (!ok) {
      return;
    }
    this.savingRetention = true;
    try {
      const payload = retentionUpdatePayload(
        this.retention.classes,
        this.drafts
      );
      this.retention = await updateRetentionSettings(payload);
      this.drafts = Object.fromEntries(
        this.retention.classes.map((row) => [row.record_class, row.days])
      );
      showToast('Retention saved.', 'success');
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Could not save retention',
        'danger'
      );
    } finally {
      this.savingRetention = false;
    }
  }

  private async runPreview(): Promise<void> {
    this.previewing = true;
    try {
      this.preview = await previewRetentionPurge();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Could not preview the purge',
        'danger'
      );
    } finally {
      this.previewing = false;
    }
  }

  private async loadPicker(type: string): Promise<void> {
    this.picker = [];
    if (type !== 'execution' && type !== 'approval') {
      return;
    }
    try {
      const path =
        type === 'execution'
          ? '/api/v1/flows/executions?limit=25'
          : '/api/v1/approval-requests?limit=25';
      const { fetchWithAuth } = await import('../../../api');
      const response = await fetchWithAuth(path);
      if (!response.ok) {
        return;
      }
      const rows = (await response.json()) as Record<string, unknown>[];
      if (!Array.isArray(rows)) {
        return;
      }
      this.picker = rows
        .filter((row) => typeof row.id === 'string')
        .map((row) => ({
          id: String(row.id),
          label:
            type === 'approval'
              ? `${row.tool_name || 'Approval'} · ${row.status || ''} · ${row.id}`
              : `${row.status || 'Execution'} · ${row.start_time || ''} · ${row.id}`,
        }));
    } catch {
      this.picker = [];
    }
  }

  private async placeHold(): Promise<void> {
    const reason = this.placeReason.trim();
    if (!isUuid(this.placeId)) {
      this.placeError = 'Enter a valid resource id.';
      return;
    }
    if (reason.length < MIN_HOLD_REASON) {
      this.placeError = `A reason of at least ${MIN_HOLD_REASON} characters is required.`;
      return;
    }
    this.placing = true;
    this.placeError = null;
    try {
      await createLegalHold({
        resource_type: this.placeType,
        resource_id: this.placeId.trim(),
        reason,
      });
      this.placeOpen = false;
      this.placeReason = '';
      this.placeId = '';
      showToast('Legal hold placed.', 'success');
      await this.loadHolds();
    } catch (err) {
      this.placeError =
        err instanceof Error ? err.message : 'Could not place the hold';
    } finally {
      this.placing = false;
    }
  }

  private async confirmRelease(): Promise<void> {
    if (!this.releaseTarget) {
      return;
    }
    const reason = this.releaseReason.trim();
    if (reason.length < MIN_HOLD_REASON) {
      this.releaseError = `A reason of at least ${MIN_HOLD_REASON} characters is required.`;
      return;
    }
    this.releasing = true;
    this.releaseError = null;
    try {
      await releaseLegalHold(this.releaseTarget.id, reason);
      this.releaseTarget = null;
      this.releaseReason = '';
      showToast(
        'Hold released. The purge may remove the record on a later pass.',
        'success'
      );
      await this.loadHolds();
    } catch (err) {
      this.releaseError =
        err instanceof Error ? err.message : 'Could not release the hold';
    } finally {
      this.releasing = false;
    }
  }

  private async exportPeriod(): Promise<void> {
    if (this.exporting) {
      return;
    }
    if (
      !this.exportStart ||
      !this.exportEnd ||
      this.exportEnd <= this.exportStart
    ) {
      this.exportError = 'End must be after start. End is exclusive.';
      return;
    }
    const span =
      (Date.parse(`${this.exportEnd}T00:00:00Z`) -
        Date.parse(`${this.exportStart}T00:00:00Z`)) /
      86400000;
    if (span > MAX_EXPORT_DAYS) {
      this.exportError = `A period export covers at most ${MAX_EXPORT_DAYS} days.`;
      return;
    }
    this.exporting = true;
    this.exportError = null;
    this.exportResult = null;
    try {
      const file = await createPeriodExport(this.exportStart, this.exportEnd);
      downloadBlob(file.blob, file.filename);
      this.exportResult = {
        filename: file.filename,
        sizeBytes: file.sizeBytes,
        keyId: file.headers.signingKeyId,
        signature: file.headers.signature,
      };
    } catch (err) {
      this.exportError = err instanceof Error ? err.message : 'Export failed';
    } finally {
      this.exporting = false;
    }
  }

  private fact(label: string, value: unknown) {
    return html`<dt>${label}</dt>
      <dd>${value ?? 'None'}</dd>`;
  }

  private renderIntegrity() {
    if (!this.canAudit) {
      return html`<p class="note">
        Audit integrity needs the View Audit Logs permission.
      </p>`;
    }
    const status = this.status;
    const range = this.selectedRange();
    const activeKey = status?.active_key_id || this.keys?.active_key_id || null;
    return html`
      <section id="audit-integrity" class="section content-card">
        <h2>Audit integrity</h2>
        <p class="honesty" data-testid="chain-honesty">${CHAIN_HONESTY}</p>
        ${
          this.statusError
            ? html`<p class="note">${this.statusError}</p>`
            : !status
              ? html`<sl-spinner></sl-spinner>`
              : html`<dl class="facts" data-testid="chain-status">
                  ${this.fact('Enabled', status.enabled ? 'Yes' : 'No')}
                  ${this.fact('Sealed rows', status.sealed_rows)}
                  ${this.fact(
                    'Unsealed rows',
                    html`${status.unsealed_rows}
                      <span class="note">
                        Seal lag ${status.seal_lag_seconds}s. Sealing is a
                        background pass and this count lags the writes.
                      </span>`
                  )}
                  ${this.fact('Last sealed at', status.last_sealed_at || 'Never')}
                  ${this.fact(
                    'Pruned below',
                    `${status.pruned_below_seq} (retention purge, not tampering)`
                  )}
                  ${this.fact('Checkpoint interval', status.checkpoint_interval)}
                  ${this.fact(
                    'Latest checkpoint',
                    status.latest_checkpoint
                      ? `seq ${status.latest_checkpoint.seq} at ${status.latest_checkpoint.checkpointed_at}, key ${status.latest_checkpoint.signing_key_id || 'none'}`
                      : 'None yet'
                  )}
                </dl>`
        }
        <div class="row-actions">
          <sl-select
            label="Range"
            size="small"
            value=${this.verifyMode}
            @sl-change=${(event: Event) => {
              this.verifyMode = (event.target as HTMLInputElement).value as
                'recent' | 'whole' | 'custom';
              if (this.status && this.verifyMode !== 'custom') {
                const next =
                  this.verifyMode === 'whole'
                    ? wholeChainRange(this.status)
                    : defaultVerifyRange(this.status);
                this.rangeStart = String(next.start);
                this.rangeEnd = String(next.end);
              }
            }}
          >
            <sl-option value="recent"
              >Last ${DEFAULT_VERIFY_ROWS.toLocaleString()} rows</sl-option
            >
            <sl-option value="whole">Whole chain</sl-option>
            <sl-option value="custom">Custom sequence range</sl-option>
          </sl-select>
          <sl-input
            label="Start sequence"
            size="small"
            data-testid="verify-start"
            .value=${this.rangeStart}
            ?disabled=${this.verifyMode !== 'custom'}
            @sl-input=${(event: Event) => {
              this.rangeStart = (event.target as HTMLInputElement).value;
            }}
          ></sl-input>
          <sl-input
            label="End sequence"
            size="small"
            data-testid="verify-end"
            .value=${this.rangeEnd}
            ?disabled=${this.verifyMode !== 'custom'}
            @sl-input=${(event: Event) => {
              this.rangeEnd = (event.target as HTMLInputElement).value;
            }}
          ></sl-input>
          <sl-button
            variant="primary"
            data-testid="verify-chain"
            ?loading=${this.verifying}
            @click=${() => this.verify()}
            >Verify chain</sl-button
          >
        </div>
        ${
          this.verifyMode === 'whole'
            ? html`<p class="warn">
                A whole-chain walk can take a long time. The server stops at its
                row limit and marks the result as cut short.
              </p>`
            : nothing
        }
        <p class="note">
          Server-side verification. It asks this deployment what it believes.
          The offline walk below is the one an auditor trusts.
        </p>
        ${
          this.verifyError
            ? html`<p class="note">${this.verifyError}</p>`
            : nothing
        }
        ${
          this.verdict
            ? html`<div data-testid="verify-result">
                <p>
                  <strong
                    >${
                      this.verdict.status === 'ok'
                        ? 'Intact'
                        : this.verdict.status === 'broken'
                          ? 'Broken'
                          : 'Empty'
                    }</strong
                  >
                  checked ${this.verdict.checked_rows} rows, sequences
                  ${this.verdict.start_seq} to ${this.verdict.end_seq}.
                  Checkpoints verified: ${this.verdict.checkpoints_verified}.
                  ${this.verdict.truncated ? 'The range was cut short.' : ''}
                </p>
                ${
                  this.verdict.first_break
                    ? html`<p data-testid="first-break">
                        First break at sequence ${this.verdict.first_break.seq},
                        row ${this.verdict.first_break.row_id || 'unknown'},
                        ${this.verdict.first_break.kind}:
                        ${this.verdict.first_break.detail}
                      </p>`
                    : nothing
                }
                <p class="honesty">${CHAIN_HONESTY}</p>
              </div>`
            : nothing
        }
        <h3>Verify offline</h3>
        <p class="note">
          The CLI recomputes every hash on your machine. Key
          ${activeKey || 'unavailable'}. Download that public key and keep it
          somewhere this deployment cannot write. The command does not take a
          key flag; checkpoints are checked against keys the CLI fetches, so a
          copy you already hold is what an auditor compares.
        </p>
        <pre class="command" data-testid="offline-command">
${offlineAuditCommand(range)}</pre>
        ${
          activeKey && this.keys
            ? html`<sl-button
                size="small"
                data-testid="download-public-key"
                @click=${() => {
                  const key = this.keys?.keys.find(
                    (row) => row.key_id === activeKey
                  );
                  if (key) {
                    downloadText(`${key.key_id}.pub`, key.public_key);
                  }
                }}
                >Download public key</sl-button
              >`
            : nothing
        }
        <h3>Checkpoints</h3>
        ${
          this.checkpoints.length === 0
            ? html`<div class="empty-state">No checkpoints yet.</div>`
            : html`<table class="styled-table">
                <thead>
                  <tr>
                    <th>Seq</th>
                    <th>Row count</th>
                    <th>Checkpointed at</th>
                    <th>Key id</th>
                    <th>Signature</th>
                  </tr>
                </thead>
                <tbody>
                  ${this.checkpoints.map(
                    (row) =>
                      html`<tr>
                        <td>${row.seq}</td>
                        <td>${row.row_count}</td>
                        <td>${row.checkpointed_at}</td>
                        <td>${row.signing_key_id || 'None'}</td>
                        <td>
                          ${
                            row.signature
                              ? html`${truncateMiddle(row.signature)}
                                  <sl-copy-button
                                    value=${row.signature}
                                  ></sl-copy-button>`
                              : 'None'
                          }
                        </td>
                      </tr>`
                  )}
                </tbody>
              </table>`
        }
        ${
          this.checkpointsDone
            ? nothing
            : html`<sl-button
                size="small"
                @click=${() => this.loadCheckpoints(false)}
                >Newer checkpoints</sl-button
              >`
        }
      </section>
    `;
  }

  private renderKeys() {
    if (!this.canAudit) {
      return nothing;
    }
    const keys = this.keys?.keys ?? [];
    return html`
      <section id="signing-keys" class="section content-card">
        <h2>Signing keys</h2>
        ${this.keysError ? html`<p class="note">${this.keysError}</p>` : nothing}
        ${
          keys.length === 0
            ? html`<div class="empty-state">
                No signing key has been published.
              </div>`
            : html`<table class="styled-table">
                <thead>
                  <tr>
                    <th>Key id</th>
                    <th>Algorithm</th>
                    <th>Created</th>
                    <th>Status</th>
                    <th>Public half</th>
                  </tr>
                </thead>
                <tbody>
                  ${keys.map(
                    (key) =>
                      html`<tr>
                        <td>${key.key_id}</td>
                        <td>${key.algorithm}</td>
                        <td>${key.created_at || 'Unknown'}</td>
                        <td>
                          ${key.active ? 'Active' : `Retired ${key.retired_at || ''}`}
                        </td>
                        <td>
                          <code>${truncateMiddle(key.public_key, 24)}</code>
                          <sl-copy-button
                            value=${key.public_key}
                          ></sl-copy-button>
                          <sl-button
                            size="small"
                            variant="text"
                            @click=${() => downloadText(`${key.key_id}.pub`, key.public_key)}
                            >Download .pub</sl-button
                          >
                        </td>
                      </tr>`
                  )}
                </tbody>
              </table>`
        }
        ${
          this.canManage
            ? html`<div class="row-actions">
                <sl-button
                  class="danger-gap"
                  variant="danger"
                  outline
                  data-testid="rotate-key"
                  ?loading=${this.rotating}
                  @click=${() => this.rotate()}
                  >Rotate key</sl-button
                >
              </div>`
            : html`<p class="note">
                Rotating a key needs the Manage Policies permission.
              </p>`
        }
      </section>
    `;
  }

  private renderRetention() {
    if (!this.canPolicies) {
      return html`<p class="note">
        Retention needs the View Policies permission.
      </p>`;
    }
    const settings = this.retention;
    const diff = settings ? retentionDiff(settings.classes, this.drafts) : [];
    return html`
      <section id="retention" class="section content-card">
        <h2>Retention</h2>
        ${
          this.retentionError
            ? html`<p class="note">${this.retentionError}</p>`
            : !settings
              ? html`<sl-spinner></sl-spinner>`
              : html`
                  <p class="note">
                    Floor ${settings.floor_days} days. Default
                    ${settings.default_days}. Maximum ${settings.max_days}.
                    Values below the floor are raised before they are saved.
                  </p>
                  <table class="styled-table">
                    <thead>
                      <tr>
                        <th>Record class</th>
                        <th>Days</th>
                        <th>Source</th>
                        <th>Floored</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${settings.classes.map((row) => {
                        const editable = retentionRowEditable(row);
                        return html`<tr>
                            <td>${row.label}</td>
                            <td>
                              ${
                                editable
                                  ? html`<sl-input
                                      type="number"
                                      size="small"
                                      data-testid=${`retention-${row.record_class}`}
                                      min=${settings.floor_days}
                                      max=${settings.max_days}
                                      .value=${String(
                                        this.drafts[row.record_class] ??
                                          row.days
                                      )}
                                      @sl-change=${(event: Event) =>
                                        this.setDraft(
                                          row.record_class,
                                          (event.target as HTMLInputElement)
                                            .value
                                        )}
                                    ></sl-input>`
                                  : html`<span
                                      >${row.days === -1 ? 'Unlimited' : row.days}</span
                                    >`
                              }
                            </td>
                            <td>${row.source}</td>
                            <td>${row.floored ? 'Yes' : 'No'}</td>
                          </tr>
                          ${
                            !editable
                              ? html`<tr>
                                  <td colspan="4" class="note">
                                    ${
                                      row.days === -1
                                        ? 'Unlimited because a subscription history promise prevents deletion. This row is read-only.'
                                        : 'This class follows a subscription history promise and cannot be shortened here.'
                                    }
                                  </td>
                                </tr>`
                              : nothing
                          }`;
                      })}
                    </tbody>
                  </table>
                  ${
                    this.canManage
                      ? html`<div class="row-actions">
                          <sl-button
                            variant="primary"
                            data-testid="save-retention"
                            ?disabled=${diff.length === 0}
                            ?loading=${this.savingRetention}
                            @click=${() => this.saveRetention()}
                            >Save retention</sl-button
                          >
                        </div>`
                      : html`<p class="note">
                          Saving retention needs the Manage Policies permission.
                        </p>`
                  }
                  <h3>Purge status</h3>
                  <p class="note">
                    Set by the deployment. This page cannot start a purge.
                  </p>
                  <dl class="facts">
                    ${this.fact('Enabled', settings.purge_enabled ? 'Yes' : 'No')}
                    ${this.fact('Dry run', settings.purge_dry_run ? 'Yes' : 'No')}
                    ${this.fact('UTC window', settings.purge_window_utc || 'Any hour')}
                    ${this.fact(
                      'Evidence payload hours',
                      settings.evidence_payload_hours
                    )}
                  </dl>
                  <sl-button
                    size="small"
                    data-testid="preview-purge"
                    ?loading=${this.previewing}
                    @click=${() => this.runPreview()}
                    >Preview purge</sl-button
                  >
                  ${
                    this.preview
                      ? html`<table
                          class="styled-table"
                          data-testid="purge-preview"
                        >
                          <thead>
                            <tr>
                              <th>Class</th>
                              <th>Cutoff</th>
                              <th>Purgeable</th>
                            </tr>
                          </thead>
                          <tbody>
                            ${this.preview.classes.map(
                              (row) =>
                                html`<tr>
                                  <td>${row.label}</td>
                                  <td>${row.cutoff || 'None'}</td>
                                  <td>${row.purgeable}</td>
                                </tr>`
                            )}
                          </tbody>
                        </table>`
                      : nothing
                  }
                `
        }
      </section>
    `;
  }

  private renderHolds() {
    if (!this.canPolicies) {
      return nothing;
    }
    return html`
      <section id="legal-holds" class="section content-card">
        <h2>Legal holds</h2>
        <p class="note">${HOLD_DOES} ${HOLD_DOES_NOT} ${OBJECT_LOCK_NOTE}</p>
        <div class="row-actions">
          ${
            this.canManage
              ? html`<sl-button
                  variant="primary"
                  data-testid="open-place-hold"
                  @click=${() => {
                    this.placeOpen = true;
                    this.placeError = null;
                    void this.loadPicker(this.placeType);
                  }}
                  >Place hold</sl-button
                >`
              : html`<p class="note">
                  Placing or releasing a hold needs the Manage Policies
                  permission.
                </p>`
          }
          <sl-checkbox
            ?checked=${this.showReleased}
            @sl-change=${(event: Event) => {
              this.showReleased = (event.target as HTMLInputElement).checked;
              void this.loadHolds();
            }}
            >Show released</sl-checkbox
          >
        </div>
        ${this.holdsError ? html`<p class="note">${this.holdsError}</p>` : nothing}
        ${
          this.holds.length === 0
            ? html`<div class="empty-state" data-testid="holds-empty">
                No legal holds. ${HOLD_DOES} ${HOLD_DOES_NOT}
              </div>`
            : html`<table class="styled-table" data-testid="holds-table">
                <thead>
                  <tr>
                    <th>Resource</th>
                    <th>Id</th>
                    <th>Reason</th>
                    <th>Placed by</th>
                    <th>Placed at</th>
                    <th>Released</th>
                    <th>Status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  ${this.holds.map((hold) => {
                    const href = holdResourceHref(
                      hold.resource_type,
                      hold.resource_id
                    );
                    return html`<tr>
                      <td>${hold.resource_type}</td>
                      <td>
                        ${
                          href
                            ? html`<a href=${href}>${hold.resource_id}</a>`
                            : html`<span
                                >${hold.resource_id}
                                <span class="note"
                                  >Evidence packs open from the execution that
                                  produced them.</span
                                ></span
                              >`
                        }
                      </td>
                      <td>${hold.reason}</td>
                      <td>${hold.placed_by_user_id || 'Unknown'}</td>
                      <td>${hold.placed_at || 'Unknown'}</td>
                      <td>
                        ${
                          hold.released_at
                            ? `${hold.released_at} by ${hold.released_by_user_id || 'unknown'}`
                            : 'Still in force'
                        }
                      </td>
                      <td>${hold.active ? 'Active' : 'Released'}</td>
                      <td>
                        ${
                          hold.active && this.canManage
                            ? html`<sl-button
                                size="small"
                                variant="danger"
                                outline
                                data-testid=${`release-${hold.id}`}
                                @click=${() => {
                                  this.releaseTarget = hold;
                                  this.releaseReason = '';
                                  this.releaseError = null;
                                }}
                                >Release</sl-button
                              >`
                            : nothing
                        }
                      </td>
                    </tr>`;
                  })}
                </tbody>
              </table>`
        }
        <sl-dialog
          label="Place legal hold"
          ?open=${this.placeOpen}
          @sl-after-hide=${() => {
            this.placeOpen = false;
          }}
        >
          <p>${HOLD_DOES} ${HOLD_DOES_NOT}</p>
          <sl-select
            label="Resource type"
            value=${this.placeType}
            @sl-change=${(event: Event) => {
              this.placeType = (event.target as HTMLInputElement).value;
              this.placeId = '';
              void this.loadPicker(this.placeType);
            }}
          >
            ${RESOURCE_TYPES.map(
              (type) =>
                html`<sl-option value=${type.value}>${type.label}</sl-option>`
            )}
          </sl-select>
          ${
            this.picker.length
              ? html`<sl-select
                  label="Recent records"
                  placeholder="Choose one, or paste an id below"
                  @sl-change=${(event: Event) => {
                    this.placeId = (event.target as HTMLInputElement).value;
                  }}
                >
                  ${this.picker.map(
                    (row) =>
                      html`<sl-option value=${row.id}>${row.label}</sl-option>`
                  )}
                </sl-select>`
              : nothing
          }
          <sl-input
            label="Resource id"
            data-testid="place-resource-id"
            .value=${this.placeId}
            @sl-input=${(event: Event) => {
              this.placeId = (event.target as HTMLInputElement).value;
            }}
          ></sl-input>
          <sl-textarea
            label="Reason"
            data-testid="place-reason"
            maxlength=${MAX_HOLD_REASON}
            help-text=${`At least ${MIN_HOLD_REASON} characters.`}
            .value=${this.placeReason}
            @sl-input=${(event: Event) => {
              this.placeReason = (event.target as HTMLInputElement).value;
            }}
          ></sl-textarea>
          ${this.placeError ? html`<p class="note">${this.placeError}</p>` : nothing}
          <sl-button slot="footer" @click=${() => (this.placeOpen = false)}
            >Cancel</sl-button
          >
          <sl-button
            slot="footer"
            variant="primary"
            data-testid="confirm-place-hold"
            ?loading=${this.placing}
            @click=${() => this.placeHold()}
            >Place hold</sl-button
          >
        </sl-dialog>
        <sl-dialog
          label="Release legal hold"
          ?open=${this.releaseTarget !== null}
          @sl-after-hide=${() => {
            if (!this.releasing) this.releaseTarget = null;
          }}
        >
          <p>
            Releasing the hold lets the next purge remove this record if it is
            past retention. The ciphertext can expire again. The reason is
            written to the audit log. A second hold on the same bytes, for
            example a pack hold beside an execution hold, stays in force.
          </p>
          <sl-textarea
            label="Reason"
            data-testid="release-reason"
            .value=${this.releaseReason}
            @sl-input=${(event: Event) => {
              this.releaseReason = (event.target as HTMLInputElement).value;
            }}
          ></sl-textarea>
          ${this.releaseError ? html`<p class="note">${this.releaseError}</p>` : nothing}
          <sl-button slot="footer" @click=${() => (this.releaseTarget = null)}
            >Cancel</sl-button
          >
          <sl-button
            slot="footer"
            variant="danger"
            outline
            data-testid="confirm-release-hold"
            ?loading=${this.releasing}
            @click=${() => this.confirmRelease()}
            >Release hold</sl-button
          >
        </sl-dialog>
      </section>
    `;
  }

  private renderExports() {
    if (!this.canAudit) {
      return nothing;
    }
    const result = this.exportResult;
    return html`
      <section id="period-exports" class="section content-card">
        <h2>Period exports</h2>
        <p class="note">
          Start is inclusive and end is exclusive, so consecutive periods do not
          overlap. The archive is signed when it is built. A long export shows a
          spinner and is not started again.
        </p>
        <div class="row-actions">
          <label
            >Start
            <input
              type="date"
              data-testid="export-start"
              .value=${this.exportStart}
              @change=${(event: Event) => {
                this.exportStart = (event.target as HTMLInputElement).value;
              }}
          /></label>
          <label
            >End
            <input
              type="date"
              data-testid="export-end"
              .value=${this.exportEnd}
              @change=${(event: Event) => {
                this.exportEnd = (event.target as HTMLInputElement).value;
              }}
          /></label>
          <sl-button
            variant="primary"
            data-testid="export-period"
            ?loading=${this.exporting}
            ?disabled=${this.exporting}
            @click=${() => this.exportPeriod()}
            >Export</sl-button
          >
        </div>
        ${this.exportError ? html`<p class="note">${this.exportError}</p>` : nothing}
        ${
          result
            ? html`<div data-testid="export-result">
                <p>
                  Downloaded ${result.filename} (${result.sizeBytes} bytes).
                  Signing key ${result.keyId || 'none'}.
                  ${
                    result.signature
                      ? html`Signature ${truncateMiddle(result.signature)}.`
                      : html`No signature header was returned.`
                  }
                </p>
                <pre class="command">
${evidenceVerifyCommand(result.filename, result.keyId)}</pre>
              </div>`
            : nothing
        }
      </section>
    `;
  }

  render() {
    if (!this.permissionsReady) {
      return html`<sl-spinner></sl-spinner>`;
    }
    return html`
      <view-header
        headerText="Records"
        description="Audit integrity, signing keys, retention, legal holds, and signed period exports."
        width="wide"
      ></view-header>
      <div class="column-layout wide">
        <div class="main-column">
          <nav class="jump">
            <a href="#audit-integrity" @click=${this.onJump}>Audit integrity</a>
            <a href="#signing-keys" @click=${this.onJump}>Signing keys</a>
            <a href="#retention" @click=${this.onJump}>Retention</a>
            <a href="#legal-holds" @click=${this.onJump}>Legal holds</a>
            <a href="#period-exports" @click=${this.onJump}>Period exports</a>
          </nav>
          ${this.renderIntegrity()} ${this.renderKeys()}
          ${this.renderRetention()} ${this.renderHolds()}
          ${this.renderExports()}
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'records-view': RecordsView;
  }
}
