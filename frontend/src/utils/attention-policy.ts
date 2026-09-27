import { getPolicyNoticeSummary, type PolicyNoticeRuleSummary } from '../api';
import type { AttentionItem } from './attention';

/**
 * Attention cards for model I/O rules with the `notify` action (#959).
 *
 * One card per rule with hits in the last seven days. The fingerprint is the
 * rule plus its newest hit, so dismissing a card hides it until the rule
 * matches again. Kept out of attention.ts so the rules for other kinds stay
 * untouched.
 */

/** Where a notice card links: the model content rules on the Policies page. */
export const POLICY_NOTICE_HREF = '/console/policies';

/** Structural shape of one summary row; the API type is assignable. */
export type AttentionPolicyNotice = Pick<
  PolicyNoticeRuleSummary,
  | 'rule_id'
  | 'rule_description'
  | 'target'
  | 'count'
  | 'last_hit_id'
  | 'last_hit_at'
  | 'last_excerpt'
  | 'last_username'
>;

/** What a notice card shows when it is expanded. */
export interface AttentionPolicyNoticeEvidence {
  ruleId: string;
  target: string;
  count: number;
  lastAt: string | null;
  lastExcerpt: string | null;
  lastUsername: string | null;
}

function targetLabel(target: string): string {
  return target === 'model.response' ? 'responses' : 'requests';
}

export function policyNoticeItems(
  notices: AttentionPolicyNotice[]
): AttentionItem[] {
  return notices
    .filter((notice) => notice.rule_id && notice.count > 0)
    .map((notice) => {
      const times = notice.count === 1 ? 'once' : `${notice.count} times`;
      return {
        id: `policy:${notice.rule_id}`,
        kind: 'policy',
        severity: 'warning',
        title: notice.rule_description || notice.rule_id,
        detail: `Notify rule ${notice.rule_id} matched model ${targetLabel(
          notice.target
        )} ${times} in the last 7 days`,
        href: POLICY_NOTICE_HREF,
        at: notice.last_hit_at || null,
        action: { label: 'Review rule', href: POLICY_NOTICE_HREF },
        fingerprint: `${notice.rule_id}|${notice.last_hit_id}`,
        dismissable: true,
        evidence: {
          policyNotice: {
            ruleId: notice.rule_id,
            target: notice.target,
            count: notice.count,
            lastAt: notice.last_hit_at || null,
            lastExcerpt: notice.last_excerpt ?? null,
            lastUsername: notice.last_username ?? null,
          },
        },
      } satisfies AttentionItem;
    });
}

/**
 * The summary for the Attention inputs. A failure (403 for an operator who
 * cannot view policies, an older server without the endpoint) yields no cards
 * rather than failing the page.
 */
export async function loadPolicyNotices(): Promise<AttentionPolicyNotice[]> {
  try {
    const summary = await getPolicyNoticeSummary(7);
    return Array.isArray(summary?.rules) ? summary.rules : [];
  } catch {
    return [];
  }
}
