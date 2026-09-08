import type { ApprovalRequest, QuestionSchema } from '../types';

/**
 * Rendering helpers for structured questions (`question_schema`).
 *
 * Kept in one place so a list row, a detail page and the public token page
 * describe the same form the same way. The agent acts on the JSON; every
 * human surface needs a sentence, and a pretty-printed blob is not one.
 */

/** One answer value as a person reads it: no braces, no quotes, no nulls. */
export function formatAnswerValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) {
    const parts = value.map(formatAnswerValue).filter(Boolean);
    return parts.length > 0 ? parts.join(', ') : '(none)';
  }
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .filter(
        ([, entry]) => entry !== null && entry !== undefined && entry !== ''
      )
      .map(([name, entry]) => `${name}: ${formatAnswerValue(entry)}`)
      .join(' · ');
  }
  return String(value);
}

/** How many fields the form asks for, autofilled ones excluded. */
export function answerFieldCount(schema: QuestionSchema | null): number {
  if (!schema?.properties) return 0;
  return Object.values(schema.properties).filter(
    (field) => !field['x-autofill']
  ).length;
}

/**
 * What a list row says about a request that carries a form.
 *
 * A row cannot show the form, so it shows the size of the job: "4 items to
 * pick from, 2 fields to fill in". Null when the request has no form, so the
 * row renders exactly what it rendered before.
 */
export function questionFormSummary(
  request: Pick<ApprovalRequest, 'question_items' | 'question_schema'>
): string | null {
  const items = request.question_items ?? [];
  const fields = answerFieldCount(request.question_schema ?? null);
  if (items.length === 0 && fields === 0) return null;
  const parts: string[] = [];
  if (items.length > 0) {
    parts.push(
      `${items.length} item${items.length === 1 ? '' : 's'} to pick from`
    );
  }
  if (fields > 0) {
    parts.push(`${fields} field${fields === 1 ? '' : 's'} to fill in`);
  }
  return parts.join(', ');
}
