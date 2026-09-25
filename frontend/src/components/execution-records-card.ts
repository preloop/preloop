import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import './legal-hold-control';
import {
  downloadEvidence,
  getEvidenceStatus,
  type BinaryDownload,
  type EvidenceStatus,
} from '../records-api';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  OBJECT_LOCK_NOTE,
  downloadBlob,
  evidenceVerifyCommand,
  formatBytes,
  truncateMiddle,
} from '../utils/records-format';

const STATUS_LABEL: Record<string, string> = {
  available: 'Present',
  missing: 'Missing',
  expired: 'Expired',
  failed: 'Failed',
};

@customElement('execution-records-card')
export class ExecutionRecordsCard extends LitElement {
  @property({ attribute: 'execution-id' })
  executionId = '';

  @state() private evidence: EvidenceStatus | null = null;
  @state() private error: string | null = null;
  @state() private downloading = false;
  @state() private downloadNote: string | null = null;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .facts {
        display: grid;
        grid-template-columns: minmax(8rem, 12rem) 1fr;
      }
      .facts dt,
      .facts dd {
        margin: 0;
        padding: var(--sl-spacing-x-small) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .facts dt,
      .note {
        color: var(--console-meta-color);
        font-size: var(--sl-font-size-small);
      }
      .actions {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small);
        align-items: center;
        margin-top: var(--sl-spacing-medium);
      }
      @media (max-width: 640px) {
        .facts {
          grid-template-columns: 1fr;
        }
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  updated(changed: Map<string, unknown>): void {
    if (changed.has('executionId') && this.executionId) {
      void this.load();
    }
  }

  private async load(): Promise<void> {
    if (!this.executionId) {
      return;
    }
    try {
      this.evidence = await getEvidenceStatus(this.executionId);
      this.error = null;
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Could not load records';
    }
  }

  private async download(): Promise<void> {
    if (this.downloading || !this.executionId) {
      return;
    }
    this.downloading = true;
    this.downloadNote = null;
    try {
      const file: BinaryDownload = await downloadEvidence(this.executionId);
      downloadBlob(file.blob, file.filename);
      const state =
        file.headers.evidenceIntegrityState ||
        file.headers.evidenceIntegrity ||
        'not reported';
      this.downloadNote = `Integrity header: ${state}. SHA-256 ${
        file.headers.evidenceSha256 || 'not reported'
      }. ${evidenceVerifyCommand(file.filename, file.headers.signingKeyId)}`;
    } catch (err) {
      this.downloadNote =
        err instanceof Error ? err.message : 'Could not download the pack';
    } finally {
      this.downloading = false;
    }
  }

  render() {
    const evidence = this.evidence;
    const label = evidence
      ? STATUS_LABEL[evidence.status] || evidence.status
      : 'Unknown';
    return html`
      <sl-card class="content-card" data-testid="execution-records">
        <h2 slot="header">Records</h2>
        ${this.error ? html`<p class="note">${this.error}</p>` : nothing}
        ${
          evidence
            ? html`<dl class="facts">
                <dt>Evidence pack</dt>
                <dd>${label}</dd>
                <dt>Size</dt>
                <dd>${formatBytes(evidence.size_bytes)}</dd>
                <dt>SHA-256</dt>
                <dd>
                  ${
                    evidence.sha256
                      ? truncateMiddle(evidence.sha256, 24)
                      : 'None'
                  }
                </dd>
                <dt>Integrity</dt>
                <dd>
                  ${evidence.integrity || 'Unknown'}
                  <span class="note">${evidence.integrity_note || ''}</span>
                </dd>
                <dt>Legal hold</dt>
                <dd>${evidence.legal_hold ? 'Yes' : 'No'}</dd>
                <dt>Object lock</dt>
                <dd>False. ${OBJECT_LOCK_NOTE}</dd>
                <dt>Expires at</dt>
                <dd>${evidence.expires_at || 'None'}</dd>
              </dl>`
            : nothing
        }
        <div class="actions">
          <sl-button
            size="small"
            data-testid="download-evidence"
            ?loading=${this.downloading}
            ?disabled=${evidence?.status !== 'available'}
            @click=${() => this.download()}
            >Download evidence</sl-button
          >
          <legal-hold-control
            resource-type="execution"
            resource-id=${this.executionId}
            ?known-held=${evidence?.legal_hold === true}
            @records-hold-changed=${() => this.load()}
          ></legal-hold-control>
          <a href="/console/audit">Audit timeline for this account</a>
        </div>
        ${this.downloadNote ? html`<p class="note">${this.downloadNote}</p>` : nothing}
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'execution-records-card': ExecutionRecordsCard;
  }
}
