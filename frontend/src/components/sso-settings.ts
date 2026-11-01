import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getFeatures, getUserProfile, getTeams, getRoles } from '../api';
import type { Team, Role } from '../types';
import { ConsoleStatus } from '../controllers/console-status';
import { confirmDialog, showToast } from './confirm-dialog';
import {
  ssoApi,
  type SamlMetadataPreview,
  type SsoConnection,
  type SsoDomain,
  type SsoPolicy,
  type SsoMapping,
  type ScimToken,
} from '../services/sso-api';
import consoleStyles from '../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';

@customElement('sso-settings')
export class SsoSettings extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private loading = true;
  @state() private enabled = false;
  @state() private allowed = false;
  @state() private busy = false;
  @state() private error = '';
  @state() private connections: SsoConnection[] = [];
  @state() private domains: SsoDomain[] = [];
  @state() private policy: SsoPolicy | null = null;
  @state() private mappings: SsoMapping[] = [];
  @state() private tokens: ScimToken[] = [];
  @state() private teams: Team[] = [];
  @state() private roles: Role[] = [];
  @state() private choicesError = '';
  @state() private loaded = false;
  @state() private editor: Partial<SsoConnection> | null = null;
  @state() private mappingConnection = '';
  @state() private newToken = '';
  @state() private showSigningKey = false;
  private newTokenId = '';
  private newTokenConnection = '';
  @state() private metadataPreview: SamlMetadataPreview | null = null;
  private accountId = '';
  private generation = 0;
  private mappingGeneration = 0;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
      }
      section {
        margin-bottom: var(--sl-spacing-x-large);
      }
      h2 {
        font-size: var(--console-text-card-title);
        margin: 0 0 1rem;
      }
      p,
      small {
        color: var(--console-meta-color);
      }
      form {
        display: grid;
        gap: 1rem;
        margin: 1rem 0;
      }
      .fields {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
        gap: 1rem;
      }
      label {
        display: grid;
        gap: 0.375rem;
      }
      input,
      textarea,
      select {
        box-sizing: border-box;
        width: 100%;
        font: inherit;
        padding: 0.625rem;
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-small);
        background: var(--console-page);
        color: var(--sl-color-neutral-900);
      }
      textarea {
        min-height: 6rem;
        resize: vertical;
      }
      input[type='checkbox'] {
        width: auto;
      }
      .check {
        display: flex;
        align-items: center;
        gap: 0.5rem;
      }
      .row {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.75rem;
        padding: 1rem 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .identity {
        flex: 1;
        min-width: 160px;
      }
      .actions {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
      }
      .danger {
        margin-left: var(--sl-spacing-large);
      }
      .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        overflow: hidden;
        clip-path: inset(50%);
        white-space: nowrap;
      }
      .copy {
        display: flex;
        align-items: center;
        overflow-wrap: anywhere;
        gap: 0.5rem;
      }
      .error {
        color: var(--sl-color-danger-700);
      }
      .empty {
        min-height: 72px;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      details {
        padding: 1rem 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      summary {
        cursor: pointer;
      }
      a {
        color: var(--console-link-color);
      }
      code {
        overflow-wrap: anywhere;
      }
    `,
  ];

  private clearNewToken(): void {
    this.newToken = '';
    this.newTokenId = '';
    this.newTokenConnection = '';
  }

  private async revokeToken(token: ScimToken): Promise<void> {
    const generation = this.generation;
    await this.confirmRemove('Revoke SCIM token', async () => {
      await ssoApi.revokeToken(this.accountId, token.id);
      if (this.current(generation) && this.newTokenId === token.id)
        this.clearNewToken();
    });
  }

  private readonly accountChanged = () => {
    this.busy = false;
    this.clearNewToken();
    this.editor = null;
    this.metadataPreview = null;
    this.showSigningKey = false;
    this.connections = [];
    this.domains = [];
    this.tokens = [];
    this.mappings = [];
    this.mappingConnection = '';
    this.allowed = false;
    this.accountId = '';
    this.loaded = false;
    void this.load();
  };

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('auth-change', this.accountChanged);
    void this.load();
  }

  disconnectedCallback(): void {
    window.removeEventListener('auth-change', this.accountChanged);
    this.generation++;
    this.mappingGeneration++;
    this.clearNewToken();
    this.editor = null;
    this.metadataPreview = null;
    this.showSigningKey = false;
    this.allowed = false;
    this.accountId = '';
    super.disconnectedCallback();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading = true;
    this.loaded = false;
    this.clearNewToken();
    this.error = '';
    try {
      const features = await getFeatures();
      if (!this.current(generation)) return;
      this.enabled = features.features.enterprise_sso === true;
      if (!this.enabled) return;
      const user = await getUserProfile();
      if (!this.current(generation)) return;
      this.allowed =
        user.is_account_owner === true ||
        user.is_superuser === true ||
        user.permissions?.includes('manage_account') === true;
      if (!this.allowed) return;
      this.accountId = user.account_id;
      await this.refresh(generation);
      if (!this.current(generation)) return;
      this.loaded = true;
      const [teams, roles] = await Promise.allSettled([getTeams(), getRoles()]);
      if (!this.current(generation)) return;
      this.teams =
        teams.status === 'fulfilled' ? (teams.value.teams ?? []) : [];
      this.roles =
        roles.status === 'fulfilled' ? (roles.value.roles ?? []) : [];
      this.choicesError =
        teams.status === 'rejected' || roles.status === 'rejected'
          ? 'Some team or role choices could not be loaded. Refresh settings to try again.'
          : '';
    } catch {
      if (this.current(generation))
        this.error =
          'Single sign-on settings could not be loaded. Refresh to try again.';
    } finally {
      if (this.current(generation)) this.loading = false;
    }
  }

  private current(generation: number): boolean {
    return this.isConnected && generation === this.generation;
  }

  private async refresh(generation: number): Promise<void> {
    const [connections, domains, policy, tokens] = await Promise.all([
      ssoApi.connections(this.accountId),
      ssoApi.domains(this.accountId),
      ssoApi.policy(this.accountId),
      ssoApi.tokens(this.accountId),
    ]);
    if (!this.current(generation)) return;
    this.connections = connections;
    this.domains = domains;
    this.policy = policy;
    this.tokens = tokens;
    if (
      this.mappingConnection &&
      !connections.some((row) => row.id === this.mappingConnection)
    ) {
      this.mappingGeneration++;
      this.mappingConnection = '';
      this.mappings = [];
    }
  }

  private async run(
    operation: (generation: number) => Promise<void>
  ): Promise<void> {
    if (this.busy || !this.allowed || !this.accountId || !this.loaded) return;
    const generation = this.generation;
    this.busy = true;
    this.error = '';
    try {
      await operation(generation);
    } catch (error) {
      if (this.current(generation))
        this.error =
          error instanceof Error
            ? error.message
            : 'The change could not be saved.';
    } finally {
      if (this.current(generation)) this.busy = false;
    }
  }

  private async confirmRemove(
    label: string,
    operation: () => Promise<unknown>
  ): Promise<void> {
    const generation = this.generation;
    const confirmed = await confirmDialog({
      title: label,
      message: `${label}? This change takes effect immediately.`,
      confirmLabel: label,
      variant: 'danger',
    });
    if (!confirmed || !this.current(generation)) return;
    await this.run(async (current) => {
      await operation();
      if (!this.current(current)) return;
      await this.refresh(current);
      if (this.current(current) && this.mappingConnection)
        await this.loadMappings(this.mappingConnection);
      if (this.current(current))
        showToast('Single sign-on settings updated.', 'success');
    });
  }

  private field(
    name: string,
    label: string,
    value = '',
    type = 'text',
    required = false
  ) {
    return html`<label
      >${label}<input
        name=${name}
        type=${type}
        .value=${value}
        ?required=${required}
        ?disabled=${this.busy}
        autocomplete=${type === 'password' ? 'new-password' : 'off'}
    /></label>`;
  }

  private async saveConnection(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    const value = (key: string) => String(data.get(key) ?? '').trim();
    const existing = this.editor;
    if (!existing) return;
    const body: Record<string, unknown> = {
      name: value('name'),
      enabled: data.has('enabled'),
      default_role: value('default_role'),
      jit_enabled: data.has('jit_enabled'),
      groups_claim: value('groups_claim') || null,
    };
    if (String(body.default_role).trim().toLowerCase() === 'owner') {
      this.error = 'Single sign-on cannot grant the owner role.';
      return;
    }
    if (!existing.id) body.protocol = existing.protocol;
    try {
      const mapping: unknown = JSON.parse(value('attribute_mapping') || '{}');
      if (
        !mapping ||
        Array.isArray(mapping) ||
        typeof mapping !== 'object' ||
        Object.values(mapping).some((v) => typeof v !== 'string')
      )
        throw new Error();
      body.attribute_mapping = mapping;
    } catch {
      this.error = 'Attribute mapping must be a JSON object of claim names.';
      return;
    }
    if (existing.protocol === 'oidc') {
      body.oidc_issuer = value('oidc_issuer');
      body.oidc_client_id = value('oidc_client_id');
      body.oidc_scopes = value('oidc_scopes').split(/\s+/).filter(Boolean);
      if (data.get('oidc_client_secret'))
        body.oidc_client_secret = String(data.get('oidc_client_secret'));
    } else {
      body.saml_idp_entity_id = value('saml_idp_entity_id');
      body.saml_idp_sso_url = value('saml_idp_sso_url');
      body.saml_idp_metadata_url = value('saml_idp_metadata_url') || null;
      body.saml_idp_certs =
        value('saml_idp_certs').match(
          /-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g
        ) ?? [];
      body.saml_want_assertions_signed = data.has(
        'saml_want_assertions_signed'
      );
      if (data.get('saml_sp_signing_key'))
        body.saml_sp_signing_key = String(data.get('saml_sp_signing_key'));
    }
    // Remove submitted secret values from the DOM even if the request fails.
    form
      .querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('[data-secret]')
      .forEach((input) => (input.value = ''));
    await this.run(async (generation) => {
      await ssoApi.saveConnection(this.accountId, existing.id ?? null, body);
      if (!this.current(generation)) return;
      this.editor = null;
      this.metadataPreview = null;
      this.showSigningKey = false;
      await this.refresh(generation);
      if (this.current(generation)) showToast('Connection saved.', 'success');
    });
  }

  private async loadMappings(connection: string): Promise<void> {
    this.mappingConnection = connection;
    this.mappings = [];
    const generation = this.generation;
    const request = ++this.mappingGeneration;
    try {
      const rows = connection
        ? await ssoApi.mappings(this.accountId, connection)
        : [];
      if (this.current(generation) && request === this.mappingGeneration)
        this.mappings = rows;
    } catch {
      if (this.current(generation) && request === this.mappingGeneration)
        this.error = 'Group mappings could not be loaded.';
    }
  }

  private async addMapping(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const data = new FormData(event.currentTarget as HTMLFormElement);
    const role = String(data.get('role') ?? '').trim();
    const team = String(data.get('team_id') ?? '').trim();
    if ((!role && !team) || role.toLowerCase() === 'owner') {
      this.error = 'Choose a team or a role other than owner.';
      return;
    }
    const connection = this.mappingConnection;
    if (!connection) return;
    await this.run(async (generation) => {
      await ssoApi.addMapping(this.accountId, connection, {
        idp_group: String(data.get('idp_group')).trim(),
        role: role || null,
        team_id: team || null,
      });
      if (this.current(generation)) await this.loadMappings(connection);
    });
  }

  private get enforceable(): boolean {
    return (
      this.domains.some((d) => d.verified) &&
      this.connections.some(
        (c) => c.enabled && c.current_admin_test_valid === true
      )
    );
  }

  private async toggleEnforcement(): Promise<void> {
    if (!this.policy || (!this.policy.enforce && !this.enforceable)) return;
    const policy = this.policy;
    const generation = this.generation;
    if (
      !policy.enforce &&
      !(await confirmDialog({
        title: 'Require single sign-on',
        message:
          'Members on verified domains will need single sign-on. All account owners retain emergency password or passkey sign-in.',
        detail:
          'Confirm that you have tested sign-in with your identity provider and can recover access as an owner.',
        confirmLabel: 'Require single sign-on',
      }))
    )
      return;
    if (!this.current(generation)) return;
    await this.run(async (current) => {
      const updated = await ssoApi.savePolicy(this.accountId, {
        enforce: !policy.enforce,
        break_glass_owner_ids: policy.break_glass_owner_ids,
        session_max_age_seconds: policy.session_max_age_seconds,
      });
      if (this.current(current)) this.policy = updated;
    });
  }

  private async testSignIn(row: SsoConnection): Promise<void> {
    await this.run(async (generation) => {
      const url = await ssoApi.testSignIn(this.accountId, row.id);
      if (this.current(generation)) window.location.assign(url);
    });
  }

  private renderMetadataImport() {
    return html`<fieldset>
      <legend>Import identity provider metadata</legend>
      ${this.field('metadata_url', 'Metadata URL', '', 'url')}
      <label
        >Or metadata XML<textarea
          name="metadata_xml"
          ?disabled=${this.busy}
        ></textarea>
      </label>
      <sl-button
        ?disabled=${this.busy}
        @click=${(event: Event) => this.previewMetadata(event)}
        >Preview metadata</sl-button
      >
      ${
        this.metadataPreview
          ? html`<p>
                Review before applying. Save the connection to persist these
                values.
              </p>
              <p>${this.metadataPreview.saml_idp_entity_id}</p>
              <p>${this.metadataPreview.saml_idp_sso_url}</p>
              <pre>${this.metadataPreview.saml_idp_certs.join('\n')}</pre>
              <sl-button
                ?disabled=${this.busy}
                @click=${() => {
                  const form =
                    this.shadowRoot!.querySelector<HTMLFormElement>(
                      '#connection-form'
                    )!;
                  const preview = this.metadataPreview!;
                  for (const [name, value] of Object.entries(preview)) {
                    const input = form.elements.namedItem(name) as
                      HTMLInputElement | HTMLTextAreaElement;
                    input.value = Array.isArray(value)
                      ? value.join('\n')
                      : value;
                  }
                  this.metadataPreview = null;
                  this.showSigningKey = false;
                }}
                >Apply metadata to editor</sl-button
              >`
          : nothing
      }
    </fieldset>`;
  }

  private async previewMetadata(event: Event): Promise<void> {
    const form = (event.currentTarget as HTMLElement).closest('form')!;
    const data = new FormData(form);
    const url = String(data.get('metadata_url') ?? '').trim();
    const xml = String(data.get('metadata_xml') ?? '').trim();
    this.metadataPreview = null;
    this.showSigningKey = false;
    if (!!url === !!xml) {
      this.error = 'Provide either a metadata URL or XML.';
      return;
    }
    const editor = this.editor;
    await this.run(async (generation) => {
      const preview = await ssoApi.importSamlMetadata(
        this.accountId,
        url ? { url } : { xml }
      );
      if (this.current(generation) && this.editor === editor)
        this.metadataPreview = preview;
    });
  }

  private renderEditor() {
    const row = this.editor;
    if (!row) return nothing;
    return html`<form id="connection-form" @submit=${this.saveConnection}>
      <h3>
        ${row.id ? 'Edit' : 'Add'} ${row.protocol === 'oidc' ? 'OIDC' : 'SAML'}
        connection
      </h3>
      <div class="fields">
        ${this.field('name', 'Connection name', row.name, 'text', true)}
        ${this.field('default_role', 'Default role', row.default_role ?? 'member', 'text', true)}
        ${this.field('groups_claim', 'Groups claim or attribute', row.groups_claim ?? '')}
        ${
          row.protocol === 'oidc'
            ? html`
                ${this.field('oidc_issuer', 'Issuer URL', row.oidc_issuer ?? '', 'url', true)}
                ${this.field('oidc_client_id', 'Client ID', row.oidc_client_id ?? '', 'text', true)}
                <label
                  >Client secret
                  ${row.oidc_client_secret_set ? '(leave blank to keep saved secret)' : ''}<input
                    data-secret
                    name="oidc_client_secret"
                    type="password"
                    autocomplete="new-password"
                    ?disabled=${this.busy}
                /></label>
                ${this.field('oidc_scopes', 'Scopes (space separated)', row.oidc_scopes?.join(' ') ?? 'openid email profile')}
              `
            : html`
                ${this.renderMetadataImport()}
                ${this.field('saml_idp_entity_id', 'Identity provider entity ID', row.saml_idp_entity_id ?? '', 'text', true)}
                ${this.field('saml_idp_sso_url', 'Identity provider sign-in URL', row.saml_idp_sso_url ?? '', 'url', true)}
                ${this.field('saml_idp_metadata_url', 'Identity provider metadata URL (optional)', row.saml_idp_metadata_url ?? '', 'url')}
                <label
                  >Identity provider certificates (PEM; include all active
                  certificates for rotation)<textarea
                    name="saml_idp_certs"
                    .value=${row.saml_idp_certs?.join('\n') ?? ''}
                    ?disabled=${this.busy}
                  ></textarea>
                </label>
                <label
                  >SP signing key (PEM; leave blank to keep saved key)<textarea
                    data-secret
                    name="saml_sp_signing_key"
                    style=${this.showSigningKey ? '' : '-webkit-text-security: disc;'}
                    autocomplete="off"
                    ?disabled=${this.busy}
                  ></textarea>
                </label>
                <sl-button
                  ?disabled=${this.busy}
                  @click=${() => {
                    this.showSigningKey = !this.showSigningKey;
                  }}
                  >${this.showSigningKey ? 'Hide signing key' : 'Show signing key'}</sl-button
                >
                <label class="check"
                  ><input
                    type="checkbox"
                    name="saml_want_assertions_signed"
                    .checked=${row.saml_want_assertions_signed !== false}
                    ?disabled=${this.busy}
                  />Require signed assertions</label
                >
              `
        }
      </div>
      <label
        >Attribute mapping (JSON; email, first_name, last_name,
        display_name)<textarea
          name="attribute_mapping"
          .value=${JSON.stringify(row.attribute_mapping ?? {})}
          ?disabled=${this.busy}
        ></textarea>
      </label>
      <label class="check"
        ><input
          type="checkbox"
          name="jit_enabled"
          .checked=${row.jit_enabled !== false}
          ?disabled=${this.busy}
        />Create an account membership on first sign-in</label
      >
      <label class="check"
        ><input
          type="checkbox"
          name="enabled"
          .checked=${row.enabled === true}
          ?disabled=${this.busy}
        />Enable connection</label
      >
      <div class="actions">
        <sl-button type="submit" variant="primary" ?loading=${this.busy}
          >Save connection</sl-button
        ><sl-button ?disabled=${this.busy} @click=${() => (this.editor = null)}
          >Cancel</sl-button
        >
      </div>
    </form>`;
  }

  private copyButton(label: string, value: string) {
    // Shoelace copy-label names its tooltip; slotted text names the button.
    return html`<sl-copy-button .value=${value} copy-label=${label}>
      <span slot="copy-icon" aria-hidden="true">⧉</span>
      <span slot="copy-icon" class="sr-only">${label}</span>
    </sl-copy-button>`;
  }

  private copyUrl(label: string, value: string | null | undefined) {
    // URL display is safe text; navigation only uses a validated local start URL.
    return value
      ? html`<div class="copy">
          <span>${label}: <code>${value}</code></span
          >${this.copyButton(`Copy ${label}`, value)}
        </div>`
      : nothing;
  }

  private renderConnections() {
    return html`<section aria-labelledby="connections-title">
      <h2 id="connections-title">Identity providers</h2>
      <p>Configure your organization's OIDC or SAML identity provider.</p>
      ${
        this.connections.length
          ? this.connections.map(
              (row) =>
                html`<details>
                  <summary>
                    ${row.name} · ${row.protocol.toUpperCase()} ·
                    ${row.enabled ? 'Enabled' : 'Disabled'}
                  </summary>
                  ${row.oidc_client_secret_set ? html`<p>Client secret saved · ending ${row.oidc_client_secret_last4}</p>` : nothing}
                  ${row.saml_sp_signing_key_set ? html`<p>SP signing key saved</p>` : nothing}
                  ${this.copyUrl('Callback URL', row.oidc_callback_url)}
                  ${this.copyUrl('SP metadata URL', row.saml_metadata_url)}
                  ${this.copyUrl('ACS URL', row.saml_acs_url)}
                  ${
                    row.saml_sp_certificate
                      ? html`<p>Public SP certificate</p>
                          <pre>${row.saml_sp_certificate}</pre>
                          ${this.copyButton('Copy public SP certificate', row.saml_sp_certificate)}`
                      : nothing
                  }
                  ${!row.oidc_callback_url && row.protocol === 'oidc' ? html`<p>The callback URL will appear when sign-in is available.</p>` : nothing}
                  ${!row.saml_metadata_url && row.protocol === 'saml' ? html`<p>SP metadata and ACS URLs will appear when SAML sign-in is available.</p>` : nothing}
                  <div class="actions">
                    <sl-button
                      ?disabled=${this.busy}
                      @click=${() => {
                        this.metadataPreview = null;
                        this.showSigningKey = false;
                        this.editor = { ...row };
                      }}
                      >Edit connection</sl-button
                    >
                    <sl-button
                      @click=${() => this.testSignIn(row)}
                      ?disabled=${this.busy || !row.enabled || row.test_sign_in_method !== 'POST' || !row.test_sign_in_url}
                      >Test sign-in</sl-button
                    >
                    <sl-button
                      class="danger"
                      variant="danger"
                      outline
                      ?disabled=${this.busy}
                      @click=${() => this.confirmRemove('Delete connection', () => ssoApi.deleteConnection(this.accountId, row.id))}
                      >Delete</sl-button
                    >
                  </div>
                  ${!row.test_sign_in_url ? html`<p>Test sign-in is unavailable until sign-in is supported for this connection.</p>` : nothing}
                </details>`
            )
          : html`<p class="empty">No identity providers configured.</p>`
      }
      <div class="actions">
        <sl-button
          ?disabled=${this.busy}
          @click=${() => {
            this.metadataPreview = null;
            this.showSigningKey = false;
            this.editor = { protocol: 'oidc' };
          }}
          >Add OIDC connection</sl-button
        ><sl-button
          ?disabled=${this.busy}
          @click=${() => {
            this.metadataPreview = null;
            this.showSigningKey = false;
            this.editor = { protocol: 'saml' };
          }}
          >Add SAML connection</sl-button
        >
      </div>
      ${this.renderEditor()}
    </section>`;
  }

  private renderDomains() {
    return html`<section aria-labelledby="domains-title">
      <h2 id="domains-title">Verified domains</h2>
      <p>
        Prove ownership with a DNS TXT record before routing employees to single
        sign-on. Public email domains cannot be claimed.
      </p>
      ${
        this.domains.length
          ? this.domains.map(
              (domain) =>
                html`<div class="row">
                  <div class="identity">
                    <strong>${domain.domain}</strong>
                    ${
                      domain.verified
                        ? html`<sl-badge pill variant="success"
                            >Verified</sl-badge
                          >`
                        : html`<sl-badge pill variant="neutral"
                            >Awaiting verification</sl-badge
                          >`
                    }
                    ${
                      !domain.verified
                        ? html`<p>
                              Add a TXT record at ${domain.txt_record_name}:
                            </p>
                            <div class="copy">
                              <code>${domain.txt_record_value}</code
                              >${this.copyButton('Copy DNS TXT value', domain.txt_record_value)}
                            </div>
                            <small
                              >DNS changes may take time to propagate.</small
                            >`
                        : nothing
                    }
                  </div>
                  <sl-button
                    ?disabled=${this.busy || domain.verified}
                    @click=${() =>
                      this.run(async (generation) => {
                        await ssoApi.verifyDomain(this.accountId, domain.id);
                        if (this.current(generation))
                          await this.refresh(generation);
                      })}
                    >Verify DNS</sl-button
                  ><sl-button
                    class="danger"
                    variant="danger"
                    outline
                    ?disabled=${this.busy}
                    @click=${() => this.confirmRemove('Remove domain', () => ssoApi.deleteDomain(this.accountId, domain.id))}
                    >Remove</sl-button
                  >
                </div>`
            )
          : html`<p class="empty">No domains claimed.</p>`
      }
      <form
        id="domain-form"
        @submit=${(event: SubmitEvent) => {
          event.preventDefault();
          const form = event.currentTarget as HTMLFormElement;
          if (!form.reportValidity()) return;
          const domain = String(new FormData(form).get('domain')).trim();
          void this.run(async (generation) => {
            await ssoApi.addDomain(this.accountId, domain);
            if (this.current(generation)) {
              form.reset();
              await this.refresh(generation);
            }
          });
        }}
      >
        ${this.field('domain', 'Email domain', '', 'text', true)}<sl-button
          type="submit"
          ?loading=${this.busy}
          >Add domain</sl-button
        >
      </form>
    </section>`;
  }

  private renderMappings() {
    return html`<section aria-labelledby="mappings-title">
      <h2 id="mappings-title">Group mappings</h2>
      <p>
        Map identity provider groups to an existing team, a role, or both.
        Ownership cannot be granted through single sign-on.
      </p>
      ${this.choicesError ? html`<p>${this.choicesError}</p>` : nothing}
      <label
        >Connection<select
          aria-label="Mapping connection"
          .value=${this.mappingConnection}
          ?disabled=${this.busy}
          @change=${(event: Event) => this.loadMappings((event.target as HTMLSelectElement).value)}
        >
          <option value="">Choose a connection</option>
          ${this.connections.map((row) => html`<option value=${row.id}>${row.name}</option>`)}
        </select></label
      >
      ${
        this.mappingConnection
          ? html`${this.mappings.map(
                (mapping) =>
                  html`<div class="row">
                    <span class="identity"
                      >${mapping.idp_group} → ${mapping.role ?? ''}
                      ${mapping.team_id ? (this.teams.find((team) => team.id === mapping.team_id)?.name ?? 'Unavailable team') : ''}</span
                    ><sl-button
                      variant="danger"
                      outline
                      ?disabled=${this.busy}
                      @click=${() =>
                        this.confirmRemove('Remove group mapping', () =>
                          ssoApi.deleteMapping(
                            this.accountId,
                            this.mappingConnection,
                            mapping.id
                          )
                        )}
                      >Remove</sl-button
                    >
                  </div>`
              )}
              <form id="mapping-form" @submit=${this.addMapping}>
                <div class="fields">
                  ${this.field('idp_group', 'Identity provider group', '', 'text', true)}<label
                    >Role (optional)<select name="role" ?disabled=${this.busy}>
                      <option value="">No role</option>
                      <option value="member">member</option>
                      ${this.roles.filter((role) => !['owner', 'member'].includes(role.name.toLowerCase())).map((role) => html`<option value=${role.name}>${role.name}</option>`)}
                    </select></label
                  >
                  <label
                    >Team (optional)<select
                      name="team_id"
                      ?disabled=${this.busy}
                    >
                      <option value="">No team</option>
                      ${this.teams.map((team) => html`<option value=${team.id}>${team.name}</option>`)}
                    </select></label
                  >
                </div>
                <sl-button type="submit" ?loading=${this.busy}
                  >Add mapping</sl-button
                >
              </form>`
          : nothing
      }
    </section>`;
  }

  private connectionLabel(row: SsoConnection): string {
    const protocol = row.protocol === 'saml' ? 'SAML' : 'OIDC';
    return `${row.name} (${protocol}${row.enabled ? '' : ', disabled'})`;
  }

  /** Whether a token can provision, from its binding as the server checks it. */
  private tokenState(
    token: ScimToken
  ): 'unbound' | 'unavailable' | 'disabled' | 'ready' {
    if (!token.connection_id) return 'unbound';
    const row = this.connections.find((c) => c.id === token.connection_id);
    if (!row) return 'unavailable';
    return row.enabled ? 'ready' : 'disabled';
  }

  private tokenBinding(token: ScimToken): string {
    const state = this.tokenState(token);
    if (state === 'unbound') return 'Not bound';
    if (state === 'unavailable') return 'Unavailable connection';
    const name =
      this.connections.find((row) => row.id === token.connection_id)?.name ??
      '';
    return state === 'disabled' ? `${name} (disabled)` : name;
  }

  private renderTokens() {
    const connections = this.connections;
    const single = connections.length === 1 ? connections[0].id : '';
    const newTokenConnection = this.connections.find(
      (row) => row.id === this.newTokenConnection
    );
    const active = this.tokens.filter((token) => !token.revoked_at);
    const replace = active.some((token) =>
      ['unbound', 'unavailable'].includes(this.tokenState(token))
    );
    const paused = active.some(
      (token) => this.tokenState(token) === 'disabled'
    );
    return html`<section aria-labelledby="scim-title">
      <h2 id="scim-title">SCIM tokens</h2>
      <p>
        Create a provisioning token for your identity provider. Tokens are shown
        once; save yours before closing this message. Provisioning also requires
        SCIM support on your server.
      </p>
      ${
        this.newToken
          ? html`<div role="status">
              <p>Save this token now. It cannot be shown again.</p>
              ${
                newTokenConnection
                  ? html`<p>
                      This token provisions members for
                      ${newTokenConnection.name}.
                    </p>`
                  : nothing
              }
              <div class="copy">
                <code>${this.newToken}</code
                >${this.copyButton('Copy new SCIM token', this.newToken)}
                <sl-button @click=${() => this.clearNewToken()}
                  >I have saved the token</sl-button
                >
              </div>
            </div>`
          : nothing
      }
      ${
        replace
          ? html`<p id="scim-legacy-note">
              Tokens that are not bound to a connection, or whose connection was
              removed, cannot provision. Create a replacement bound to a
              connection, update your identity provider, then revoke the old
              token.
            </p>`
          : nothing
      }
      ${
        paused
          ? html`<p>
              Tokens bound to a disabled connection cannot provision until that
              connection is enabled.
            </p>`
          : nothing
      }
      ${this.tokens.map(
        (token) =>
          html`<div class="row">
            <span class="identity"
              >${token.name} · ${token.prefix}… · ${this.tokenBinding(token)} ·
              ${token.revoked_at ? 'Revoked' : 'Active'}</span
            >${
              token.revoked_at
                ? nothing
                : ['unbound', 'unavailable'].includes(this.tokenState(token))
                  ? html`<sl-badge pill class="chip" variant="warning"
                      >Replace</sl-badge
                    >`
                  : this.tokenState(token) === 'disabled'
                    ? html`<sl-badge pill class="chip" variant="warning"
                        >Connection disabled</sl-badge
                      >`
                    : nothing
            }<sl-button
              class="danger"
              variant="danger"
              outline
              ?disabled=${this.busy || !!token.revoked_at}
              @click=${() => this.revokeToken(token)}
              >Revoke</sl-button
            >
          </div>`
      )}
      ${
        connections.length
          ? nothing
          : html`<p>
              Add an OIDC or SAML connection before creating a SCIM token.
            </p>`
      }
      <form
        id="token-form"
        @submit=${(event: SubmitEvent) => {
          event.preventDefault();
          const form = event.currentTarget as HTMLFormElement;
          if (!this.connections.length || !form.reportValidity()) return;
          const data = new FormData(form);
          const name = String(data.get('name')).trim();
          const connectionId = String(data.get('connection_id') ?? '');
          if (!connectionId) return;
          void this.run(async (generation) => {
            this.clearNewToken();
            const created = await ssoApi.createToken(
              this.accountId,
              name,
              connectionId
            );
            if (this.current(generation)) {
              this.newTokenId = created.id;
              this.newTokenConnection = created.connection_id ?? connectionId;
              this.newToken = created.token;
              form.reset();
              await this.refresh(generation);
            }
          });
        }}
      >
        <div class="fields">
          ${this.field('name', 'Token name', '', 'text', true)}<label
            >Connection<select
              name="connection_id"
              required
              aria-describedby="scim-connection-help"
              ?disabled=${this.busy || !connections.length}
            >
              ${
                single
                  ? nothing
                  : html`<option value="">Choose a connection</option>`
              }
              ${connections.map(
                (row) =>
                  html`<option value=${row.id} ?selected=${row.id === single}>
                    ${this.connectionLabel(row)}
                  </option>`
              )}
            </select></label
          >
        </div>
        <small id="scim-connection-help"
          >Each token provisions members for one connection. People it creates
          sign in through that connection.</small
        ><sl-button
          type="submit"
          ?loading=${this.busy}
          ?disabled=${!!this.newToken || !connections.length}
          >Create SCIM token</sl-button
        >
      </form>
    </section>`;
  }

  render() {
    if (this.loading)
      return html`<p role="status">Loading single sign-on settings…</p>`;
    if (!this.enabled && !this.error)
      return html`<h2>Single sign-on</h2>
        <p>
          Single sign-on is available with Preloop Cloud and Preloop Enterprise.
        </p>
        <sl-button
          @click=${() => window.dispatchEvent(new CustomEvent('show-upgrade-modal', { detail: { code: 'upgrade_required', feature: 'enterprise_sso' }, bubbles: true, composed: true }))}
          >See plans</sl-button
        >`;
    if (!this.allowed)
      return html`<p role="alert">
        ${this.error || 'Only account owners and admins can manage single sign-on.'}
      </p>`;
    if (!this.loaded)
      return html`<p role="alert">
          ${this.error || 'Single sign-on settings are unavailable.'}
        </p>
        <sl-button @click=${this.load}>Refresh settings</sl-button>`;
    return html`${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>
              <sl-button ?disabled=${this.busy} @click=${this.load}
                >Refresh settings</sl-button
              >`
          : nothing
      }
      <div aria-busy=${this.busy}>
        ${this.renderConnections()}${this.renderDomains()}
        <section aria-labelledby="enforcement-title">
          <h2 id="enforcement-title">Require single sign-on</h2>
          <p>
            Require employees on verified domains to sign in with your identity
            provider. All account owners retain emergency password or passkey
            sign-in. Emergency sign-in is audited and emails all owners. Turning
            enforcement off does not restore revoked sessions.
          </p>
          <p>
            A connection must be enabled and successfully tested by you within
            the last 24 hours before enforcement can be enabled.
          </p>
          <sl-button
            id="enforcement-toggle"
            ?disabled=${this.busy || !this.policy || (!this.policy.enforce && !this.enforceable)}
            @click=${this.toggleEnforcement}
            >${this.policy?.enforce ? 'Turn off enforcement' : 'Require single sign-on'}</sl-button
          >
          ${!this.policy?.enforce && !this.enforceable ? html`<p>Verify a domain, enable a connection, and complete a successful test sign-in before enabling enforcement.</p>` : nothing}
        </section>
        ${this.renderMappings()}${this.renderTokens()}
      </div>`;
  }
}
