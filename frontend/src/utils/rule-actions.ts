/**
 * One vocabulary for what a governance rule does when it matches.
 *
 * Tools, Policies and the rule editors all show rule actions. They used to
 * spell and colour them independently ("require_approval" in blue on one
 * page, "REQUIRE APPROVAL" in amber on another), so an operator who learned
 * a colour on one page misread it on the next. Every surface that shows a
 * rule action reads its label, Shoelace variant and icon from here.
 *
 * The keys are the backend's `ConditionAction` values
 * (`backend/preloop/services/policy/schema.py`).
 */

export type RuleActionVariant =
  'success' | 'danger' | 'warning' | 'primary' | 'neutral';

export interface RuleActionMeta {
  /** Sentence-case label, for badges and option text. */
  label: string;
  /** Shoelace variant; also names the colour token family (`--sl-color-<variant>-*`). */
  variant: RuleActionVariant;
  /** Shoelace icon name. */
  icon: string;
  /** One line on what happens to the call, for help text and tooltips. */
  description: string;
}

export const RULE_ACTION_META: Readonly<Record<string, RuleActionMeta>> = {
  allow: {
    label: 'Allow',
    variant: 'success',
    icon: 'check-circle-fill',
    description: 'The call runs.',
  },
  deny: {
    label: 'Deny',
    variant: 'danger',
    icon: 'x-octagon-fill',
    description: 'The call is blocked.',
  },
  require_approval: {
    label: 'Require approval',
    variant: 'warning',
    icon: 'shield-lock-fill',
    description: 'The call waits for a person to approve it.',
  },
  notify: {
    label: 'Notify',
    variant: 'primary',
    icon: 'bell-fill',
    description: 'The call runs and the policy owners are told.',
  },
  redact: {
    label: 'Redact',
    variant: 'neutral',
    icon: 'eraser-fill',
    description: 'The call runs and stored copies are redacted.',
  },
};

/**
 * The display metadata for a rule action.
 *
 * An action this console does not know yet still renders: its name is
 * turned into sentence case ("rate_limit" -> "Rate limit") with a neutral
 * badge, rather than showing raw snake_case.
 */
export function ruleActionMeta(
  action: string | null | undefined
): RuleActionMeta {
  const key = (action ?? '').trim();
  const known = RULE_ACTION_META[key];
  if (known) return known;
  const words = key.replace(/[_-]+/g, ' ').trim().toLowerCase();
  return {
    label: words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Unknown',
    variant: 'neutral',
    icon: 'question-circle',
    description: '',
  };
}

/** Sentence-case label for a rule action. */
export function ruleActionLabel(action: string | null | undefined): string {
  return ruleActionMeta(action).label;
}
