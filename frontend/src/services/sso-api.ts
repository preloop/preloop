import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';

export interface SsoConnection {
  id: string;
  account_id: string;
  protocol: 'oidc' | 'saml';
  name: string;
  enabled: boolean;
  oidc_issuer: string | null;
  oidc_client_id: string | null;
  oidc_client_secret_set: boolean;
  oidc_client_secret_last4: string;
  oidc_scopes: string[];
  groups_claim: string | null;
  saml_idp_entity_id: string | null;
  saml_idp_sso_url: string | null;
  saml_idp_metadata_url: string | null;
  saml_idp_certs: string[];
  saml_sp_signing_key_set: boolean;
  saml_want_assertions_signed: boolean;
  default_role: string;
  jit_enabled: boolean;
  attribute_mapping: Record<string, string>;
  // Fields are omitted by older servers.
  test_sign_in_url?: string | null;
  oidc_callback_url?: string | null;
  test_sign_in_method?: 'POST';
  current_admin_test_valid?: boolean;
  saml_metadata_url?: string | null;
  saml_sp_certificate?: string | null;
  saml_acs_url?: string | null;
}

export interface SsoDomain {
  id: string;
  domain: string;
  verified: boolean;
  verified_at: string | null;
  txt_record_name: string;
  txt_record_value: string;
}

export interface SsoPolicy {
  enforce: boolean;
  break_glass_owner_ids: string[] | null;
  session_max_age_seconds: number | null;
}

export interface SamlMetadataPreview {
  saml_idp_entity_id: string;
  saml_idp_sso_url: string;
  saml_idp_certs: string[];
}

export interface SsoMapping {
  id: string;
  connection_id: string;
  idp_group: string;
  team_id: string | null;
  role: string | null;
}

export interface ScimToken {
  id: string;
  name: string;
  prefix: string;
  /** Null for tokens created before tokens were bound to a connection; they cannot provision. */
  connection_id?: string | null;
  created_at: string;
  last_used_at?: string | null;
  revoked_at: string | null;
}

/** Only backend-controlled local SSO paths may navigate an authenticated browser. */
export function localSsoUrl(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value, window.location.origin);
    if (
      url.origin !== window.location.origin ||
      url.username ||
      url.password ||
      url.hash ||
      !url.pathname.startsWith(`${REST_API_PREFIX}/auth/sso/`)
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}

/** Discovery uses no stored credential and never triggers token refresh. */
export async function discoverSso(email: string): Promise<string | null> {
  const response = await fetch(`${REST_API_PREFIX}/auth/sso/discover`, {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });
  if (response.status === 404) return null;
  if (!response.ok)
    throw new Error(
      'Unable to check single sign-on. Try again or use another sign-in method.'
    );
  const body: { sso_url?: unknown } = await response.json();
  return localSsoUrl(body.sso_url);
}

async function request<T>(
  accountId: string,
  path: string,
  method = 'GET',
  body?: unknown
): Promise<T> {
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/accounts/${encodeURIComponent(accountId)}/sso${path}`,
    {
      method,
      cache: 'no-store',
      ...(body === undefined
        ? {}
        : {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }),
    }
  );
  if (!response.ok) {
    if (response.status === 422 && path === '/policy')
      throw new Error(
        'Complete a fresh test sign-in with the current connection configuration, verify a domain, and try again.'
      );
    // Validation payloads may include user input; never render them or log them.
    if (response.status === 403)
      throw new Error(
        'Only account owners and admins can manage single sign-on.'
      );
    if (response.status === 404)
      throw new Error('Single sign-on is unavailable for this account.');
    if (response.status === 409)
      throw new Error('This name or verified domain is already in use.');
    if (response.status === 429)
      throw new Error('Please wait before trying again.');
    throw new Error(
      'The change could not be saved. Check the configuration and try again.'
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

const connectionPath = (id: string) => `/connections/${encodeURIComponent(id)}`;

export const ssoApi = {
  testSignIn: async (account: string, id: string): Promise<string> => {
    const result = await request<{ sso_url?: unknown }>(
      account,
      `${connectionPath(id)}/test-sign-in`,
      'POST'
    );
    const url = localSsoUrl(result.sso_url);
    if (
      !url ||
      new URL(url).pathname !==
        `${REST_API_PREFIX}/auth/sso/${encodeURIComponent(id)}/start`
    )
      throw new Error(
        'Test sign-in could not be started. Refresh settings and try again.'
      );
    return url;
  },
  importSamlMetadata: (
    account: string,
    source: { url: string } | { xml: string }
  ) =>
    request<SamlMetadataPreview>(
      account,
      '/saml/metadata/import',
      'POST',
      source
    ),
  connections: (account: string) =>
    request<SsoConnection[]>(account, '/connections'),
  saveConnection: (
    account: string,
    id: string | null,
    body: Record<string, unknown>
  ) =>
    request<SsoConnection>(
      account,
      id ? connectionPath(id) : '/connections',
      id ? 'PATCH' : 'POST',
      body
    ),
  deleteConnection: (account: string, id: string) =>
    request<void>(account, connectionPath(id), 'DELETE'),
  domains: (account: string) => request<SsoDomain[]>(account, '/domains'),
  addDomain: (account: string, domain: string) =>
    request<SsoDomain>(account, '/domains', 'POST', { domain }),
  verifyDomain: (account: string, id: string) =>
    request<SsoDomain>(
      account,
      `/domains/${encodeURIComponent(id)}/verify`,
      'POST'
    ),
  deleteDomain: (account: string, id: string) =>
    request<void>(account, `/domains/${encodeURIComponent(id)}`, 'DELETE'),
  policy: (account: string) => request<SsoPolicy>(account, '/policy'),
  savePolicy: (
    account: string,
    policy: Pick<
      SsoPolicy,
      'enforce' | 'break_glass_owner_ids' | 'session_max_age_seconds'
    >
  ) => request<SsoPolicy>(account, '/policy', 'PUT', policy),
  mappings: (account: string, connection: string) =>
    request<SsoMapping[]>(account, `${connectionPath(connection)}/mappings`),
  addMapping: (
    account: string,
    connection: string,
    body: { idp_group: string; team_id: string | null; role: string | null }
  ) =>
    request<SsoMapping>(
      account,
      `${connectionPath(connection)}/mappings`,
      'POST',
      body
    ),
  deleteMapping: (account: string, connection: string, id: string) =>
    request<void>(
      account,
      `${connectionPath(connection)}/mappings/${encodeURIComponent(id)}`,
      'DELETE'
    ),
  tokens: (account: string) => request<ScimToken[]>(account, '/scim-tokens'),
  createToken: (account: string, name: string, connectionId: string) =>
    request<ScimToken & { token: string }>(account, '/scim-tokens', 'POST', {
      name,
      connection_id: connectionId,
    }),
  revokeToken: (account: string, id: string) =>
    request<void>(account, `/scim-tokens/${encodeURIComponent(id)}`, 'DELETE'),
};
