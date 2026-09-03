/**
 * Quoting for the shell commands the console offers an operator to copy.
 *
 * Several places print a command that embeds an agent's display name
 * (`preloop agents onboard <name>`, `preloop agents install-plugin <name>`).
 * The name is whatever the operator called their agent, so it can contain a
 * space, a quote, `$(...)` or a backtick, and inside double quotes the shell
 * would expand those the moment the command is pasted into a terminal.
 *
 * Single quotes are the only shell quoting with no escapes inside them, so a
 * quoted value is inert. The one thing a single-quoted string cannot contain
 * is a single quote, which is why an embedded `'` closes the string, adds an
 * escaped quote and opens it again: `'\''`.
 */
export function shellQuote(value: string | null | undefined): string {
  return `'${String(value ?? '').replace(/'/g, `'\\''`)}'`;
}
