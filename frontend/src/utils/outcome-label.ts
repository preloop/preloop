/**
 * Words for the enums the API sends. A badge that prints `success` is the
 * database talking; the console says "Succeeded". Kept here, next to the other
 * pure helpers, so a component can say what happened without importing another
 * component to do it.
 */

const ACRONYMS: Record<string, string> = {
  api: 'API',
  ai: 'AI',
  mcp: 'MCP',
  id: 'ID',
  url: 'URL',
  sso: 'SSO',
  oauth: 'OAuth',
  ip: 'IP',
};

/** `api_key_created` reads as "API key created", not "api key created". */
export function humaniseAction(action: string): string {
  const words = (action || '')
    .replace(/[_-]+/g, ' ')
    .trim()
    .split(/\s+/)
    .map((word) => ACRONYMS[word.toLowerCase()] || word);
  if (words.length === 0) return '';
  const first = words[0];
  words[0] = ACRONYMS[first.toLowerCase()]
    ? first
    : first.charAt(0).toUpperCase() + first.slice(1);
  return words.join(' ');
}

/**
 * Enum values that a badge would otherwise print raw: `success`, `error`,
 * `timeout`. The activity feed says what happened in the past tense, so a
 * badge on a session request or an activity row says the same word the feed
 * does ("Succeeded", not "success"). Anything outside this list falls back to
 * `humaniseAction`, so a value the console has never seen is still shown.
 */
const OUTCOME_LABELS: Record<string, string> = {
  success: 'Succeeded',
  succeeded: 'Succeeded',
  ok: 'Succeeded',
  completed: 'Completed',
  error: 'Failed',
  failed: 'Failed',
  failure: 'Failed',
  denied: 'Denied',
  budget_denied: 'Denied by budget',
  declined: 'Declined',
  timeout: 'Timed out',
  cancelled: 'Cancelled',
  canceled: 'Cancelled',
  running: 'Running',
  pending: 'Pending',
  started: 'Started',
  executed: 'Executed',
  info: 'Info',
};

/** A status or outcome enum as a word a reader recognises. */
export function outcomeLabel(value: string | null | undefined): string {
  const key = (value || '').trim().toLowerCase();
  if (!key) return '';
  return OUTCOME_LABELS[key] || humaniseAction(key);
}
