/**
 * Console client for one account's session embedding opt in (#791).
 *
 * Reading takes `view_runtime_sessions`; saving takes `manage_budgets`,
 * because turning embedding on, widening the scope or raising the cap is a
 * spending decision. Shapes follow openapi.yaml.
 */
import { extractErrorMessage, fetchWithAuth } from './api';

/** The two scopes the server accepts. Anything else is a 422. */
export type SessionEmbeddingScope = 'summaries_only' | 'full';

/** The two providers the worker can build. */
export type SessionEmbeddingProvider = 'openai_compatible' | 'local';

export interface SessionEmbeddingCorpus {
  /** Chunks carrying a vector of any model. */
  vectors: number;
  /** Chunks embedded with the model configured now. */
  model_vectors: number;
  /** Chunks inside the current scope still waiting for a vector. */
  pending: number;
  embedded_through: string | null;
}

export interface SessionEmbeddingSetting {
  enabled: boolean;
  scope: SessionEmbeddingScope;
  scope_help: string;
  provider: SessionEmbeddingProvider | string;
  model_identifier: string | null;
  base_url: string | null;
  dimensions: number;
  /** Null falls back to `deployment_daily_cap_usd`. */
  daily_cap_usd: number | null;
  deployment_daily_cap_usd: number;
  /** False when the deployment kill switch overrides every account. */
  deployment_embedding_enabled: boolean;
  degraded_reason: string | null;
  degraded_at: string | null;
  corpus: SessionEmbeddingCorpus;
}

/**
 * What a save sends. Fields left out are left alone by the server, and so
 * is an explicit null on any field except `daily_cap_usd`.
 */
export interface SessionEmbeddingSettingUpdate {
  enabled?: boolean;
  scope?: SessionEmbeddingScope;
  /** Null clears the account cap back to the deployment default. */
  daily_cap_usd?: number | null;
  provider?: SessionEmbeddingProvider;
  model_identifier?: string;
  /** Sent as typed; an empty string is refused as base_url_required. */
  base_url?: string;
}

/** A refused read or save, with the HTTP status and the refusal code. */
export class SessionEmbeddingError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = 'SessionEmbeddingError';
    this.status = status;
    this.code = code;
  }
}

export const SESSION_EMBEDDING_SETTING_URL =
  '/api/v1/runtime-sessions/settings/embedding';

async function refusal(
  response: Response,
  fallback: string
): Promise<SessionEmbeddingError> {
  const body = await response.json().catch(() => null);
  const code =
    body && typeof body.detail === 'object' && !Array.isArray(body.detail)
      ? (body.detail?.code ?? null)
      : null;
  return new SessionEmbeddingError(
    extractErrorMessage(body, fallback),
    response.status,
    typeof code === 'string' ? code : null
  );
}

export async function getSessionEmbeddingSetting(): Promise<SessionEmbeddingSetting> {
  const response = await fetchWithAuth(SESSION_EMBEDDING_SETTING_URL);
  if (!response.ok) {
    throw await refusal(response, 'Failed to load the semantic search setting');
  }
  return response.json();
}

export async function updateSessionEmbeddingSetting(
  update: SessionEmbeddingSettingUpdate
): Promise<SessionEmbeddingSetting> {
  const response = await fetchWithAuth(SESSION_EMBEDDING_SETTING_URL, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  if (!response.ok) {
    throw await refusal(response, 'Failed to save the semantic search setting');
  }
  return response.json();
}
