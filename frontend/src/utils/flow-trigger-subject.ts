/**
 * Is a flow's trigger about an issue?
 *
 * The flow form offers "comment on the triggering issue when a pull request is
 * opened". That option only makes sense when the run has a triggering issue to
 * comment on, so the form needs one precise answer rather than a guess per
 * call site. The rule, from the trigger configuration alone:
 *
 * - Webhook, schedule and manual triggers never carry a tracker issue. A
 *   webhook body is arbitrary JSON and a schedule fires with no subject.
 * - A tracker trigger is about an issue when at least one selected event type
 *   names an issue as its subject: any `issue_*` event (GitHub, GitLab, Jira)
 *   or any `comment_*` event, because a tracker comment is written on an issue
 *   thread (Jira) or on an issue or pull/merge request (GitHub, GitLab).
 * - Pull request and merge request events, push, tag push, release,
 *   deployment, pipeline, job, check and workflow events are about code or a
 *   review, not about an issue.
 * - A tracker trigger with no event type selected yet is not about an issue:
 *   nothing has been chosen that could carry one.
 *
 * Comment events are included on purpose. "Comment `/preloop fix` on an issue"
 * is a normal way to start an implementation flow, and hiding the option there
 * would hide it from the case it was written for. The event can also be a
 * comment on a pull request, in which case the backend posts on that pull
 * request instead (`extract_trigger_comment_target` accepts both), so the
 * option is never offered where nothing could be commented on.
 */

/** The three trigger kinds the flow form can configure. */
export type FlowTriggerType = 'webhook' | 'tracker' | 'schedule';

const ISSUE_SUBJECT_PREFIXES = ['issue', 'comment'] as const;

/**
 * True when one tracker event type names an issue as its subject.
 *
 * Matches by prefix so provider-specific spellings (`issues.opened`,
 * `comment_created`, `issue_labeled`) and event names added later are
 * classified without a lookup table that would silently drop them.
 */
export function isIssueSubjectEventType(eventType: unknown): boolean {
  if (typeof eventType !== 'string') return false;
  const normalized = eventType.trim().toLowerCase();
  if (!normalized) return false;
  return ISSUE_SUBJECT_PREFIXES.some((prefix) => normalized.startsWith(prefix));
}

/**
 * True when the configured trigger is about an issue.
 *
 * @param triggerType Trigger kind selected on the form.
 * @param eventTypes  `flow.trigger_event_types` as stored, possibly undefined.
 */
export function triggerIsAboutIssue(
  triggerType: FlowTriggerType | undefined,
  eventTypes: unknown
): boolean {
  if (triggerType !== 'tracker') return false;
  if (!Array.isArray(eventTypes)) return false;
  return eventTypes.some(isIssueSubjectEventType);
}
