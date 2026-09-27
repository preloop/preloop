import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import { getUserProfile, hasPermission } from '../api';
import {
  getSessionEmbeddingSetting,
  updateSessionEmbeddingSetting,
  type SessionEmbeddingProvider,
  type SessionEmbeddingScope,
  type SessionEmbeddingSetting,
  type SessionEmbeddingSettingUpdate,
} from '../session-embedding-api';

/**
 * What each scope costs, in the numbers docs/operations/session-embedding.md
 * uses. The cost difference is the whole decision, so it is stated next to
 * the choice rather than behind a link.
 */
const SCOPE_OPTIONS: ReadonlyArray<{
  value: SessionEmbeddingScope;
  label: string;
  cost: string;
}> = [
  {
    value: 'summaries_only',
    label: 'Summaries only (default)',
    cost: "Each session's title and summary, about one chunk per session: roughly 60 MB of vectors for 10,000 sessions.",
  },
  {
    value: 'full',
    label: 'Full transcripts',
    cost: 'Every chunk, transcripts included, about 40 per session: roughly 2.4 GB of vectors for 10,000 sessions plus the search index, and about forty times the provider spend.',
  },
];

/** One sentence per degraded reason the worker records on the setting. */
const DEGRADED_SENTENCES: Record<string, string> = {
  daily_cap_reached:
    "Today's embedding spend reached the daily cap. Waiting chunks stay pending and the next day's run picks them up.",
  provider_error:
    'The embedding provider failed the last batch. The worker retries on its next pass.',
  dimension_mismatch:
    'The model returned vectors of a width the corpus does not store. Choose a model that produces 1536 dimensions.',
  misconfigured:
    'The provider is not configured usably. Check the model and the endpoint.',
  unpriced_model:
    'The model is not in the price catalogue, so it was refused before any text was sent: the daily cap cannot meter a model with no price.',
};

const PROVIDER_LABELS: Record<SessionEmbeddingProvider, string> = {
  openai_compatible: 'OpenAI compatible endpoint',
  local: 'Local model',
};

function formatCount(value: number): string {
  return value.toLocaleString('en-US');
}

function formatUsd(value: number): string {
  return value.toFixed(2);
}

/**
 * Opt this account in to semantic session search (#791).
 *
 * Reading takes `view_runtime_sessions`, so a viewer sees the whole state
 * with the controls disabled. Saving takes `manage_budgets`; without it the
 * save button is not offered at all rather than failing on click.
 *
 * A saved change is announced with `session-embedding-changed` so the
 * sessions view can re-run the search that was reporting
 * `semantic_not_enabled`.
 */
@customElement('session-embedding-settings')
export class SessionEmbeddingSettings extends LitElement {
  @state() private setting: SessionEmbeddingSetting | null = null;
  @state() private canManage = false;
  @state() private loaded = false;
  @state() private saving = false;
  @state() private error: string | null = null;
  @state() private saved = false;

  @state() private draftEnabled = false;
  @state() private draftScope: SessionEmbeddingScope = 'summaries_only';
  @state() private draftCap = '';
  @state() private draftProvider: SessionEmbeddingProvider =
    'openai_compatible';
  @state() private draftModel = '';
  @state() private draftBaseUrl = '';

  static styles = css`
    :host {
      display: block;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--sl-spacing-small);
    }
    h2 {
      margin: 0;
      font-size: var(--sl-font-size-large);
    }
    .body {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }
    .note,
    .scope-cost {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      margin: 0;
    }
    .scope-cost {
      display: block;
      margin-top: var(--sl-spacing-3x-small);
    }
    sl-radio {
      margin-bottom: var(--sl-spacing-x-small);
    }
    .provider {
      display: grid;
      gap: var(--sl-spacing-small);
    }
    .actions {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const [profile, setting] = await Promise.all([
        getUserProfile(),
        getSessionEmbeddingSetting(),
      ]);
      this.canManage = hasPermission(profile?.permissions, 'manage_budgets');
      this.adopt(setting);
    } catch {
      // A member who cannot read runtime sessions gets no card at all: the
      // search view already tells them semantic ranking did not run.
      this.setting = null;
    }
    this.loaded = true;
  }

  /** Make the server's answer the stored state and the form's starting point. */
  private adopt(setting: SessionEmbeddingSetting): void {
    this.setting = setting;
    this.draftEnabled = setting.enabled;
    this.draftScope = setting.scope;
    this.draftCap =
      setting.daily_cap_usd === null || setting.daily_cap_usd === undefined
        ? ''
        : String(setting.daily_cap_usd);
    this.draftProvider =
      setting.provider === 'local' ? 'local' : 'openai_compatible';
    this.draftModel = setting.model_identifier ?? '';
    this.draftBaseUrl = setting.base_url ?? '';
  }

  /** The body for this save, or a sentence saying why there is none. */
  private buildUpdate(): SessionEmbeddingSettingUpdate | string {
    const capText = this.draftCap.trim();
    let cap: number | null = null;
    if (capText !== '') {
      cap = Number(capText);
      if (!Number.isFinite(cap) || cap < 0) {
        return 'The daily cap must be zero or more US dollars, or empty for the deployment default.';
      }
    }
    const update: SessionEmbeddingSettingUpdate = {
      enabled: this.draftEnabled,
      scope: this.draftScope,
      daily_cap_usd: cap,
    };
    if (this.draftEnabled) {
      const model = this.draftModel.trim();
      if (!model) {
        return 'Name the embedding model before turning semantic search on.';
      }
      update.provider = this.draftProvider;
      update.model_identifier = model;
      if (this.draftProvider === 'openai_compatible') {
        // Sent as typed, empty included: the server refuses an empty
        // endpoint with base_url_required, where a null would be read as
        // "leave the stored one" and quietly reuse an endpoint the user
        // just cleared.
        update.base_url = this.draftBaseUrl.trim();
      }
    }
    return update;
  }

  private async save(): Promise<void> {
    this.error = null;
    this.saved = false;
    const update = this.buildUpdate();
    if (typeof update === 'string') {
      this.error = update;
      return;
    }
    this.saving = true;
    try {
      const setting = await updateSessionEmbeddingSetting(update);
      this.adopt(setting);
      this.saved = true;
      this.dispatchEvent(
        new CustomEvent('session-embedding-changed', {
          bubbles: true,
          composed: true,
          detail: { setting },
        })
      );
    } catch (err) {
      // The stored state is untouched: the form keeps what the user typed so
      // they can correct it, and the status still reads what the server has.
      this.error =
        err instanceof Error
          ? err.message
          : 'Failed to save the semantic search setting';
    } finally {
      this.saving = false;
    }
  }

  private renderProgress(setting: SessionEmbeddingSetting) {
    const corpus = setting.corpus;
    if (!setting.enabled && corpus.vectors === 0) {
      return html`<p class="note" data-testid="embedding-progress">
        Nothing is embedded yet. Keyword search covers every session either way.
      </p>`;
    }
    if (!setting.enabled) {
      // `pending` is scope filtered, not opt in filtered: while embedding is
      // off nothing drains it, so it must not read as a stalled backlog.
      return html`<p class="note" data-testid="embedding-progress">
        Embedding is off. ${formatCount(corpus.vectors)} chunks keep the vectors
        they already have.
        ${
          corpus.pending > 0
            ? html`${formatCount(corpus.pending)} chunks would be embedded in
              the current scope if it is turned back on.`
            : nothing
        }
      </p>`;
    }
    const otherModel = corpus.vectors - corpus.model_vectors;
    return html`<p class="note" data-testid="embedding-progress">
      ${formatCount(corpus.model_vectors)} chunks embedded with the current
      model, ${formatCount(corpus.pending)} waiting in the current scope.
      ${
        otherModel > 0
          ? html`${formatCount(otherModel)} more carry vectors from another
            model.`
          : nothing
      }
      ${
        corpus.embedded_through
          ? html`Embedded through
            ${new Date(corpus.embedded_through).toLocaleString()}.`
          : nothing
      }
    </p>`;
  }

  private renderDegraded(setting: SessionEmbeddingSetting) {
    if (!setting.degraded_reason) {
      return nothing;
    }
    const sentence =
      DEGRADED_SENTENCES[setting.degraded_reason] ??
      `The last embedding run stopped short: ${setting.degraded_reason}.`;
    return html`<sl-alert
      variant="warning"
      open
      data-testid="embedding-degraded"
    >
      <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
      ${sentence}
      ${
        setting.degraded_at
          ? html`<span class="note"
              >Recorded ${new Date(setting.degraded_at).toLocaleString()}.</span
            >`
          : nothing
      }
    </sl-alert>`;
  }

  private renderProvider() {
    if (!this.draftEnabled) {
      return nothing;
    }
    const readOnly = !this.canManage;
    return html`<div class="provider">
      <sl-select
        label="Provider"
        data-testid="embedding-provider"
        .value=${this.draftProvider}
        ?disabled=${readOnly}
        @sl-change=${(event: Event) => {
          this.draftProvider = (event.target as HTMLSelectElement)
            .value as SessionEmbeddingProvider;
        }}
      >
        ${(Object.keys(PROVIDER_LABELS) as SessionEmbeddingProvider[]).map(
          (value) =>
            html`<sl-option value=${value}
              >${PROVIDER_LABELS[value]}</sl-option
            >`
        )}
      </sl-select>
      <sl-input
        label="Model"
        data-testid="embedding-model"
        placeholder="text-embedding-3-small"
        .value=${this.draftModel}
        ?disabled=${readOnly}
        help-text="The model session text is sent to. It must produce 1536 dimensional vectors."
        @sl-input=${(event: Event) => {
          this.draftModel = (event.target as HTMLInputElement).value;
        }}
      ></sl-input>
      ${
        this.draftProvider === 'openai_compatible'
          ? html`<sl-input
              label="Endpoint"
              type="url"
              data-testid="embedding-base-url"
              placeholder="https://embeddings.example.com/v1"
              .value=${this.draftBaseUrl}
              ?disabled=${readOnly}
              help-text="An https endpoint serving /embeddings. Loopback, link-local and metadata hosts are refused."
              @sl-input=${(event: Event) => {
                this.draftBaseUrl = (event.target as HTMLInputElement).value;
              }}
            ></sl-input>`
          : nothing
      }
    </div>`;
  }

  render() {
    const setting = this.setting;
    if (!this.loaded || !setting) {
      return nothing;
    }
    const readOnly = !this.canManage;
    const deploymentCap = formatUsd(setting.deployment_daily_cap_usd);
    return html`
      <sl-card data-testid="embedding-card">
        <div slot="header" class="header">
          <h2>Semantic search</h2>
          <sl-badge
            data-testid="embedding-status"
            variant=${setting.enabled ? 'success' : 'neutral'}
            pill
            >${setting.enabled ? 'On' : 'Off'}</sl-badge
          >
        </div>
        <div class="body">
          <p class="note">
            Semantic and hybrid search rank sessions by meaning. They need this
            account's session text embedded by a model you name, which sends
            that text to the model and costs provider spend under a daily cap.
          </p>
          ${
            setting.deployment_embedding_enabled
              ? nothing
              : html`<sl-alert
                  variant="neutral"
                  open
                  data-testid="embedding-kill-switch"
                >
                  <sl-icon slot="icon" name="info-circle"></sl-icon>
                  Embedding is switched off for this deployment by an operator,
                  so nothing is embedded whatever this setting says.
                </sl-alert>`
          }
          ${this.renderDegraded(setting)} ${this.renderProgress(setting)}

          <sl-switch
            data-testid="embedding-enabled"
            ?checked=${this.draftEnabled}
            ?disabled=${readOnly}
            @sl-change=${(event: Event) => {
              this.draftEnabled = (event.target as HTMLInputElement).checked;
            }}
            >Embed this account's session content</sl-switch
          >
          ${this.renderProvider()}

          <sl-radio-group
            label="What to embed"
            data-testid="embedding-scope"
            .value=${this.draftScope}
            ?disabled=${readOnly}
            @sl-change=${(event: Event) => {
              this.draftScope = (event.target as HTMLInputElement)
                .value as SessionEmbeddingScope;
            }}
          >
            ${SCOPE_OPTIONS.map(
              (option) =>
                html`<sl-radio
                  value=${option.value}
                  ?disabled=${readOnly}
                  data-testid=${`scope-${option.value}`}
                  >${option.label}
                  <span class="scope-cost">${option.cost}</span></sl-radio
                >`
            )}
          </sl-radio-group>
          <p class="note" data-testid="scope-change-note">
            Changing the scope does not delete vectors. Switching to summaries
            only keeps what full already embedded and does not reclaim that
            storage; switching to full hands the untouched backlog to the
            worker, still under the daily cap.
          </p>

          <sl-input
            label="Daily cap (USD)"
            type="number"
            min="0"
            step="0.01"
            data-testid="embedding-cap"
            placeholder=${deploymentCap}
            .value=${this.draftCap}
            ?disabled=${readOnly}
            @sl-input=${(event: Event) => {
              this.draftCap = (event.target as HTMLInputElement).value;
            }}
          >
            <span slot="help-text" data-testid="embedding-cap-help"
              >Leave empty to use the deployment default of $${deploymentCap} a
              day. Reaching the cap pauses embedding until the next day; it is
              not an error.</span
            >
          </sl-input>

          ${
            this.error
              ? html`<sl-alert
                  variant="danger"
                  open
                  data-testid="embedding-error"
                >
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.error}
                </sl-alert>`
              : nothing
          }
          ${
            readOnly
              ? html`<p class="note" data-testid="embedding-read-only">
                  You can see this setting. Changing it needs the Manage Budgets
                  permission.
                </p>`
              : html`<div class="actions">
                  <sl-button
                    variant="primary"
                    data-testid="embedding-save"
                    ?loading=${this.saving}
                    @click=${() => this.save()}
                    >Save</sl-button
                  >
                  ${
                    this.saved
                      ? html`<span class="note" data-testid="embedding-saved"
                          >Saved.</span
                        >`
                      : nothing
                  }
                </div>`
          }
        </div>
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-embedding-settings': SessionEmbeddingSettings;
  }
}
