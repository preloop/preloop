import { ApiError, fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';

export interface MfaTokens {
  access_token: string;
  refresh_token: string;
}

let pendingChallenge = '';

/** Carry a proved email sign-in to the login view without URLs or storage. */
export function setPendingMfaChallenge(challenge: string): void {
  pendingChallenge = challenge;
}

/** A challenge handoff is read once and discarded on page reload. */
export function takePendingMfaChallenge(): string {
  const challenge = pendingChallenge;
  pendingChallenge = '';
  return challenge;
}

/** Replace the current session after enrollment or a fresh proof. */
export function storeMfaTokens(data: MfaTokens): void {
  localStorage.setItem('accessToken', data.access_token);
  localStorage.setItem('refreshToken', data.refresh_token);
}

/** Authenticated MFA calls never put a proof in a URL or browser storage. */
export async function mfaRequest(path: string, body?: object): Promise<any> {
  const response = await fetchWithAuth(`${REST_API_PREFIX}/auth/mfa/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    cache: 'no-store',
  });
  if (response.status === 204) return undefined;
  const data = await response.json();
  if (!response.ok) {
    throw new ApiError(
      data.detail?.message || 'Authentication failed.',
      response.status,
      data.detail?.code,
      data.detail
    );
  }
  return data;
}

/** Stable messages shared by login and the authenticator controls. */
export function mfaMessage(error: unknown): string {
  const messages: Record<string, string> = {
    invalid_code:
      'This code is invalid or has already been used. Try the next code.',
    challenge_expired: 'This sign-in has expired. Start again.',
    challenge_locked: 'Too many incorrect codes. Start again later.',
  };
  return error instanceof ApiError && error.code && messages[error.code]
    ? messages[error.code]
    : error instanceof Error
      ? error.message
      : 'Authentication failed. Please try again.';
}
