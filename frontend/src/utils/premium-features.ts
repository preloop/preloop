/**
 * The 402 upgrade contract, as the console reads it.
 *
 * A gated endpoint answers `{"detail": {"code": "upgrade_required",
 * "feature": "<name>"}}`. Two kinds of name arrive there: a plan capability
 * (`ai_optimization`, `price_overrides`, `rbac`), which is what a plan
 * actually sells, and the caller-facing name of the thing that was blocked
 * (`session_optimization`, `replay_verification`, `session_titles`), which is
 * what the reader recognises. This module keeps the label a reader sees and
 * the capability a plan has to include in one place, so the dialog, the plan
 * picker and the plan page cannot disagree about what an upgrade buys.
 */

/** Human-readable names for gated features (402 upgrade contract). */
export const PREMIUM_FEATURE_LABELS: Record<string, string> = {
  session_optimization: 'AI session optimization',
  replay_verification: 'replay verification',
  session_titles: 'AI session titles',
  value_reviews: 'value reviews',
  rbac: 'role-based access control',
  enterprise_sso: 'single sign-on',
  team_approvals: 'team approval workflows',
  reconciliation: 'provider billing reconciliation',
  // The three gates a person reaches by doing something: setting a price,
  // running an analysis on a built-in model, asking for older data. Without
  // a label the modal prints the raw capability key at the reader.
  price_overrides: 'custom model prices',
  ai_optimization: 'analysis with built-in models',
  analytics_window_days: 'analytics history beyond your plan window',
};

/**
 * Feature names that are not themselves plan capabilities.
 *
 * Everything that runs analysis on a built-in hosted model is sold as one
 * capability, so all three map onto it rather than onto a capability no plan
 * lists (a plan page looking for `session_optimization` in a plan's
 * capability array would find it nowhere and offer no upgrade at all).
 */
const FEATURE_CAPABILITIES: Record<string, string> = {
  session_optimization: 'ai_optimization',
  replay_verification: 'ai_optimization',
  session_titles: 'ai_optimization',
};

/** The plan capability that unlocks `feature`, or the feature itself. */
export function capabilityForFeature(feature: string): string {
  return FEATURE_CAPABILITIES[feature] ?? feature;
}

/** What to call `feature` in a sentence. Unknown names print as they arrive. */
export function premiumFeatureLabel(feature: string): string {
  return PREMIUM_FEATURE_LABELS[feature] || feature;
}

/**
 * Where "see the plans" goes.
 *
 * One constant because three surfaces (the upgrade dialog, the account page's
 * "Choose a plan", any future nudge) have to agree on it: moving the page
 * means editing this line, not hunting for string literals.
 */
export const PLAN_PAGE_PATH = '/console/settings/plan';

/**
 * The plan page URL for a gated feature.
 *
 * The feature travels in the query so the page can preselect the cheapest
 * plan that actually includes it. The dialog used to buy a hardcoded plan
 * instead, which was wrong in both directions: too much plan for a feature
 * the entry plan includes, and a refused checkout for a feature it does not.
 *
 * The name travels exactly as the 402 sent it, never as the capability it
 * maps to. Both readers of `?feature=` map it themselves before matching a
 * plan, so plan selection is identical either way, while the page keeps the
 * ability to name the refusal in the words the dialog used: a reader refused
 * "AI session titles" must not arrive at a card promising "built-in model
 * optimization", which is a phrase they were never shown.
 */
export function planPageUrl(feature?: string | null): string {
  return feature
    ? `${PLAN_PAGE_PATH}?feature=${encodeURIComponent(feature)}`
    : PLAN_PAGE_PATH;
}

/**
 * The least a plan has to state for `cheapestPlanUnlocking` to judge it.
 *
 * Deliberately structural rather than `BillingPlan`, so the rule can be
 * applied to the billing catalog and to any published plan list without this
 * module depending on either shape.
 */
export interface UnlockablePlan {
  id: string;
  price_monthly?: number | null;
  capabilities?: string[];
  purchasable?: boolean;
  legacy?: boolean;
  is_legacy?: boolean;
}

/**
 * The cheapest plan that unlocks `feature`, by monthly catalog price.
 *
 * One rule, in one place, because two surfaces of the plan page ask this
 * question about the same reader: the card the page highlights and the plan
 * the quote panel opens on. When they answered it separately they could name
 * different plans, and then the refusal and its fix were no longer one click
 * apart.
 *
 * The rule: the plan has to publish the capability (a plan with no capability
 * list cannot be proven to include it), be sold (`purchasable`), not be a
 * legacy plan the account cannot newly take, carry a real monthly price so
 * "cheapest" means something, and pass `canHold`.
 *
 * `canHold` is where each caller states what this account may actually do:
 * both callers pass the server's eligibility verdict, so a plan the account
 * is blocked from taking is never offered as the answer. Highlighting a card
 * whose button cannot be pressed answers nothing.
 */
export function cheapestPlanUnlocking(
  plans: readonly UnlockablePlan[],
  feature: string,
  canHold: (plan: UnlockablePlan) => boolean = () => true
): string {
  const capability = capabilityForFeature(feature);
  const price = (plan: UnlockablePlan): number =>
    typeof plan.price_monthly === 'number'
      ? plan.price_monthly
      : Number.POSITIVE_INFINITY;
  return (
    plans
      .filter(
        (plan) =>
          plan.purchasable !== false &&
          !(plan.is_legacy ?? plan.legacy ?? false) &&
          Number.isFinite(price(plan)) &&
          (plan.capabilities ?? []).includes(capability) &&
          canHold(plan)
      )
      .sort((a, b) => price(a) - price(b))[0]?.id ?? ''
  );
}
