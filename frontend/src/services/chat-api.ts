import { fetchWithAuth } from '../api';

export type ChatProvider = 'slack' | 'mattermost' | 'discord';

export interface ChatConnection {
  id: string;
  provider: ChatProvider;
  workspace_id: string;
  name: string;
  enabled: boolean;
  linked: boolean;
  external_user_id: string | null;
  ingress_url: string;
}

export interface ChatConnectionCreate {
  provider: ChatProvider;
  workspace_id: string;
  name: string;
  verification_secret: string;
  bot_token: string;
  bot_user_id: string;
  base_url: string;
}

export interface ChatLinkCode {
  code: string;
  expires_at: string;
  instruction: string;
}

export interface ChatDelivery {
  id: string;
  status: string;
  created_at: string;
  last_error: string | null;
}

async function chatRequest<T>(
  path: string,
  method = 'GET',
  body?: unknown
): Promise<T> {
  const response = await fetchWithAuth(`/api/v1/chat${path}`, {
    method,
    cache: 'no-store',
    ...(body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(
      typeof detail?.detail === 'string'
        ? detail.detail
        : `Chat connection request failed (${response.status})`
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export const listChatConnections = () =>
  chatRequest<{ connections: ChatConnection[]; can_manage: boolean }>(
    '/connections'
  );

export const createChatConnection = (body: ChatConnectionCreate) =>
  chatRequest<ChatConnection>('/connections', 'POST', body);

export const setChatConnectionEnabled = (id: string, enabled: boolean) =>
  chatRequest<ChatConnection>(
    `/connections/${encodeURIComponent(id)}`,
    'PATCH',
    { enabled }
  );

export const createChatLinkCode = (id: string) =>
  chatRequest<ChatLinkCode>(
    `/connections/${encodeURIComponent(id)}/link-code`,
    'POST'
  );

export const unlinkChatIdentity = (id: string) =>
  chatRequest<void>(
    `/connections/${encodeURIComponent(id)}/identity`,
    'DELETE'
  );

export const listChatDeliveries = (id: string) =>
  chatRequest<{ deliveries: ChatDelivery[] }>(
    `/connections/${encodeURIComponent(id)}/deliveries`
  );
