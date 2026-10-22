/**
 * Compact elapsed-time format shared by the transcript footer and the live
 * status line: `Ns`, then `Nm SSs`, then `Nh MMm SSs`.
 *
 * Format from openai/codex codex-rs/tui `fmt_elapsed_compact` (Apache-2.0).
 */
export function formatElapsedCompact(ms: number): string {
  const total = Math.max(0, Math.floor((Number.isFinite(ms) ? ms : 0) / 1000));
  if (total < 60) return `${total}s`;
  const pad = (value: number): string => String(value).padStart(2, '0');
  if (total < 3600) return `${Math.floor(total / 60)}m ${pad(total % 60)}s`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return `${hours}h ${pad(minutes)}m ${pad(total % 60)}s`;
}
