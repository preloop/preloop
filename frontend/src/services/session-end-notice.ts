/**
 * Why an administrator ended this browser's session, shown once on sign-in.
 *
 * The server names only the kind of change (`X-Session-End-Reason`), never
 * who made it. Session storage holds the reason code until the sign-in page
 * reads it; nothing sensitive is stored.
 */
const KEY = 'sessionEndReason';

const NOTICES: Record<string, string> = {
  sign_in_policy:
    'Your administrator now requires a second sign-in step. Sign in with your password to set it up.',
  sign_in_reset:
    'An administrator reset your sign-in. Sign in with your password and set it up again.',
};

/** Keep a known server reason for the next sign-in page. */
export function rememberSessionEnd(reason: string | null): void {
  if (reason && reason in NOTICES) sessionStorage.setItem(KEY, reason);
}

/** Return the notice for a remembered reason once, then forget it. */
export function takeSessionEndNotice(): string {
  const reason = sessionStorage.getItem(KEY);
  sessionStorage.removeItem(KEY);
  return (reason && NOTICES[reason]) || '';
}
