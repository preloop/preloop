import { REST_API_PREFIX } from '../api-path';
import { randomUuid } from './random-uuid';

/** Retry one immutable start intent after its owner grants permission. */
export async function ownerConsentRequest(
  fetcher: (path: string, init?: RequestInit) => Promise<Response>,
  path: string,
  init: RequestInit,
  timing: { pollIntervalMs?: number; maxWaitMs?: number } = {}
): Promise<Response> {
  const payload = JSON.parse(init.body as string);
  const body = JSON.stringify({
    ...payload,
    consent_request_id: payload.consent_request_id ?? newConsentRequestId(),
  });
  const request = { ...init, body };
  const response = await fetcher(path, request);
  if (response.status !== 409) return response;
  const error = await response
    .clone()
    .json()
    .catch(() => null);
  const pending = error?.detail;
  if (
    pending?.status !== 'pending' ||
    typeof pending.approval_id !== 'string'
  ) {
    return response;
  }

  const signal = init.signal;
  let deadline = Date.now() + (timing.maxWaitMs ?? 30 * 60 * 1000);
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const statusResponse = await fetcher(
      `${REST_API_PREFIX}/approval-requests/${encodeURIComponent(pending.approval_id)}/consent-status`,
      { signal, cache: 'no-store' }
    );
    if (!statusResponse.ok) return statusResponse;
    const approval = await statusResponse.json();
    if (approval.status !== 'pending') {
      // The start endpoint consumes approval exactly once and provides the
      // existing backend refusal for declined, expired or consumed intents.
      return fetcher(path, request);
    }
    if (typeof approval.expires_at === 'string') {
      const expiry = Date.parse(approval.expires_at);
      if (Number.isFinite(expiry)) deadline = Math.min(deadline, expiry);
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) return fetcher(path, request);
    await waitForConsentPoll(
      Math.min(timing.pollIntervalMs ?? 2000, remaining),
      signal
    );
  }
  // Preserve the server's pending message if the local follow limit expires.
  return response;
}

function waitForConsentPoll(
  ms: number,
  signal?: AbortSignal | null
): Promise<void> {
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      reject(signal?.reason ?? new DOMException('', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', abort, { once: true });
  });
}

function newConsentRequestId(): string {
  return randomUuid();
}
