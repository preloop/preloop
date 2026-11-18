import { ApiError, fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';

export interface MfaTokens {
  access_token: string;
  refresh_token: string;
}

/** Presentation hint only; the server authorizes every API request. */
export function enrollmentOnly(token: string | null): boolean {
  if (!token) return false;
  try {
    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(payload)).enroll_only === true;
  } catch {
    return false;
  }
}

/** Read a browser redirect challenge once, then erase proof from the address. */
export function takeRedirectMfaChallenge(): {
  challenge: string;
  cliRedirect: string | null;
} | null {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const challenge = params.get('mfa_challenge');
  if (!challenge) return null;
  const candidate = params.get('cli_redirect_uri');
  window.history.replaceState(
    {},
    '',
    window.location.pathname + window.location.search
  );
  return {
    challenge,
    cliRedirect: safeCliRedirect(candidate) ? candidate : null,
  };
}

/** Only literal loopback callbacks may receive a CLI's completed session. */
export function safeCliRedirect(value: string | null): string | null {
  if (!value || /[\\\r\n\t]/.test(value)) return null;
  try {
    const url = new URL(value);
    const loopback =
      url.hostname === 'localhost' ||
      url.hostname === '[::1]' ||
      /^127\.\d+\.\d+\.\d+$/.test(url.hostname);
    return loopback &&
      ['http:', 'https:'].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
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
    factor_unreadable:
      'Your authenticator app cannot be checked on this server because its encryption key changed. Use a recovery code instead, or ask an administrator to reset your sign-in.',
    mfa_required_by_policy:
      'Your account requires a strong sign-in method. Add another factor before removing this one.',
  };
  return error instanceof ApiError && error.code && messages[error.code]
    ? messages[error.code]
    : error instanceof Error
      ? error.message
      : 'Authentication failed. Please try again.';
}
