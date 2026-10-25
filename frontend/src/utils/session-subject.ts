/**
 * Session subjects come from the first prompt a harness sent. Harnesses
 * prepend their own plumbing to that prompt (a billing header line, wrapper
 * tags such as `<system-reminder>`), and local paths carry the operator's home
 * directory. This strips only an explicit allow-list of known wrappers so a
 * real prompt is never hidden, and shortens home paths to `~`.
 */

/** Wrapper tags harnesses inject around their own text. */
const WRAPPER_TAGS = [
  'system-reminder',
  'local-command-caveat',
  'local-command-stdout',
  'local-command-stderr',
  'command-name',
  'command-message',
  'command-args',
  'user-prompt-submit-hook',
] as const;

const BILLING_HEADER =
  /^\s*x-anthropic-billing-header:(?:\s*[\w.-]+=[^;\n]*;?)*/i;

const HOME_PATH =
  /(^|[\s"'(=])(?:\/Users|\/home)\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*/g;

/** Replace `/Users/<name>` and `/home/<name>` prefixes with `~`. */
export function shortenHomePaths(text: string): string {
  return text.replace(HOME_PATH, '$1~');
}

/**
 * Clean a session subject or preview.
 *
 * @param text Raw subject text.
 * @returns The cleaned text, or null when nothing but harness text remains.
 */
export function cleanSessionSubject(
  text: string | null | undefined
): string | null {
  if (!text) return null;
  // The billing header goes first so a wrapper right after it counts as the
  // start of the text.
  let out = text.replace(BILLING_HEADER, ' ');
  for (const tag of WRAPPER_TAGS) {
    // A closed block goes whole. An unclosed opener at the very start means
    // a truncated harness block, so the rest is harness text too; one later
    // in the text is a prompt mentioning the tag and stays.
    out = out.replace(new RegExp(`<${tag}>[\\s\\S]*?</${tag}>`, 'gi'), ' ');
    out = out.replace(new RegExp(`^\\s*<${tag}>[\\s\\S]*$`, 'i'), ' ');
  }
  out = shortenHomePaths(out).replace(/\s+/g, ' ').trim();
  return out || null;
}

/**
 * Drop a leading harness billing header line from preview text. Unlike
 * cleanSessionSubject this keeps everything else, wrappers included, since a
 * preview is evidence of what was sent.
 */
export function stripBillingHeader(text: string): string {
  return text.replace(BILLING_HEADER, '').replace(/^\s+/, '');
}
