import { html, nothing, TemplateResult } from 'lit';

/**
 * The subject of a flow execution: what this run was about.
 *
 * "Refund Assistant, 3 minutes, failed" describes five runs of the same
 * flow identically. The trigger does not: one is a pull request, the next
 * an issue, the next a scheduled sweep. The server derives that line once,
 * at execution-creation time (`sync/event_normalizer.extract_trigger_subject`),
 * and stores it on the row, so every list can show it without parsing a
 * webhook payload.
 *
 * These helpers are the console's single reading of that field, including
 * what to show when it is missing: rows created before subjects existed
 * still say something better than a UUID.
 */

/** The shape every executions list has in common. */
export interface ExecutionSubjectSource {
  id?: string | null;
  trigger_subject?: string | null;
  trigger_subject_url?: string | null;
  /** Only present on the detail endpoints; lists project the two fields. */
  trigger_event_details?: Record<string, unknown> | null;
}

/**
 * Where the server stores the subject inside the trigger snapshot.
 *
 * The list endpoint projects it into two columns (`trigger_subject`,
 * `trigger_subject_url`) to avoid loading the whole JSONB payload; the
 * detail endpoint returns the snapshot itself, subject included, and never
 * fills those two fields. Reading both keeps one renderer for both shapes.
 */
const SUBJECT_KEY = '_subject';

function storedSubject(
  details: Record<string, unknown> | null | undefined
): { text?: string; url?: string } | null {
  if (!details || typeof details !== 'object') return null;
  const stored = (details as Record<string, unknown>)[SUBJECT_KEY];
  if (!stored || typeof stored !== 'object') return null;
  const { text, url } = stored as { text?: unknown; url?: unknown };
  return {
    text: typeof text === 'string' ? text : undefined,
    url: typeof url === 'string' ? url : undefined,
  };
}

/** A short, stable handle for a run with nothing else to show. */
export function shortExecutionId(id?: string | null): string {
  return (id || '').slice(0, 8);
}

function triggerDetailsFallback(
  details: Record<string, unknown> | null | undefined
): string | null {
  if (!details || typeof details !== 'object') return null;

  const who = details.triggered_by;
  const source = String(details.source || '').toLowerCase();

  if (details.test_mode || who || source === 'manual') {
    const name = typeof who === 'string' ? who.trim() : '';
    return name ? `Manual run by ${name}` : 'Manual run';
  }
  if (source === 'schedule') return 'Scheduled';
  if (source) return 'Webhook';
  return null;
}

/**
 * The line to print for an execution, or the short id when nothing is known.
 *
 * Order: the stored subject, then whatever the trigger snapshot can tell us
 * (who ran it, that a schedule ran it, that a webhook did), then the id.
 */
export function executionSubjectText(exec: ExecutionSubjectSource): string {
  const subject = subjectOf(exec);
  if (subject) return subject;
  return (
    triggerDetailsFallback(exec.trigger_event_details) ||
    shortExecutionId(exec.id)
  );
}

/** The derived subject, from whichever shape the endpoint returned. */
function subjectOf(exec: ExecutionSubjectSource): string {
  const projected = (exec.trigger_subject || '').trim();
  if (projected) return projected;
  return (storedSubject(exec.trigger_event_details)?.text || '').trim();
}

/** The link to the thing the subject names, if the trigger carried one. */
export function executionSubjectUrl(
  exec: ExecutionSubjectSource
): string | null {
  return (
    exec.trigger_subject_url ||
    storedSubject(exec.trigger_event_details)?.url ||
    null
  );
}

/** True when the text shown is a fallback rather than a real subject. */
export function isSubjectFallback(exec: ExecutionSubjectSource): boolean {
  return !subjectOf(exec);
}

/**
 * Render the subject, linked to the thing it names when we have a URL.
 *
 * The link leaves the console (a pull request, an issue, a compare view), so
 * it opens in a new tab and says so with a trailing icon rather than with
 * text. Clicks are stopped from bubbling: these lines sit inside rows that
 * are themselves clickable.
 */
export function renderExecutionSubject(
  exec: ExecutionSubjectSource
): TemplateResult {
  const text = executionSubjectText(exec);
  const url = executionSubjectUrl(exec);
  const fallback = isSubjectFallback(exec);
  const className = `execution-subject${fallback ? ' is-fallback' : ''}`;

  if (url) {
    // The icon sits outside the ellipsised text, or a long subject clips the
    // one mark that says the link leaves the console.
    return html`<a
      class="${className} execution-subject-link"
      href=${url}
      target="_blank"
      rel="noopener noreferrer"
      title=${text}
      @click=${(event: Event) => event.stopPropagation()}
      ><span class="execution-subject-text">${text}</span
      ><sl-icon name="box-arrow-up-right" label="Opens in a new tab"></sl-icon
    ></a>`;
  }

  return html`<span class=${className} title=${text}
    ><span class="execution-subject-text">${text}</span></span
  >`;
}

/** Shared styling for the two shapes above, as a plain CSS string. */
export const executionSubjectCss = `
  .execution-subject {
    align-items: center;
    display: inline-flex;
    gap: 4px;
    max-width: 100%;
    min-width: 0;
  }
  /* The text is what gives way when the row is narrow; the icon never does. */
  .execution-subject-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .execution-subject.is-fallback {
    color: var(--console-meta-color);
  }
  a.execution-subject-link {
    color: var(--console-link-color);
    text-decoration: none;
  }
  a.execution-subject-link:hover .execution-subject-text,
  a.execution-subject-link:focus-visible .execution-subject-text {
    text-decoration: underline;
  }
  a.execution-subject-link sl-icon {
    flex-shrink: 0;
    font-size: 12px;
  }
`;

/** Keeps `nothing` importable for callers that render conditionally. */
export const SUBJECT_NOTHING = nothing;
