import type { PricingConfig, PricingComparisonRow } from './brand-config';
import type { BillingPlan } from './types/billing';

interface Catalog {
  plans: BillingPlan[];
  storage_retention?: { minimum_days?: number; legal_holds_override?: boolean };
}
const CAPABILITIES: Record<string, string> = {
  ai_optimization: 'Built-in model optimization',
  value_reviews: 'Value reviews',
  rbac: 'Role-based access control',
  team_approvals: 'Team approval workflows',
  price_overrides: 'Model price overrides',
  reconciliation: 'Provider billing reconciliation',
};
const money = (amount: number) =>
  new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
  }).format(amount);
const compact = (amount: number) =>
  new Intl.NumberFormat('en-US', {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount);

/** Build both SSR and client pricing from the exact catalog shipped to billing. */
export function applyPricingCatalog(
  pricing: PricingConfig,
  catalog: Catalog
): PricingConfig {
  if (!Array.isArray(catalog?.plans))
    throw new Error('Pricing catalog must contain plans');
  const configured = pricing.plans.filter((p) => {
    const entry = catalog.plans.find((c) => c.id === p.id);
    return !(entry?.is_legacy ?? entry?.legacy);
  });
  const plans = configured.map((p) => {
    const entry = catalog.plans.find((c) => c.id === p.id);
    if (!entry)
      throw new Error(
        `Public plan ${p.id} is missing from the billing catalog`
      );
    if (!entry.features || !Array.isArray(entry.capabilities))
      throw new Error(
        `Public plan ${p.id} has no verified limits/capabilities`
      );
    return entry;
  });
  /**
   * Quoted plans (Enterprise, anything the catalog marks unpurchasable) are
   * self-managed or dedicated deals. One predicate drives both the tab split
   * and the contact-only CTA so those cannot drift.
   */
  const isQuoted = (p: BillingPlan) =>
    p.id === 'enterprise' || p.purchasable === false;
  const catalogDeployment = (p: BillingPlan): 'cloud' | 'dedicated' =>
    isQuoted(p) ? 'dedicated' : 'cloud';
  /**
   * Brand config wins so EE brands.yaml can route a plan between tabs
   * without a catalog change. Catalog is the default when the brand leaves
   * `deployment` unset.
   */
  const deploymentOf = (
    configPlan: { deployment?: 'cloud' | 'dedicated' },
    entry: BillingPlan
  ): 'cloud' | 'dedicated' => configPlan.deployment ?? catalogDeployment(entry);
  /**
   * The comparison table describes cloud subscriptions only. A quoted plan has
   * no fixed quota to put in a cell, and the founder review rejected the fifth
   * column it produced. Honour the same brand override as the tab split.
   */
  const cloudPlans = plans.filter(
    (entry, index) => deploymentOf(configured[index], entry) === 'cloud'
  );
  const row = (
    label: string,
    value: (p: BillingPlan) => string | boolean
  ): PricingComparisonRow => ({
    label,
    values: Object.fromEntries(cloudPlans.map((p) => [p.id, value(p)])),
  });
  const number = (p: BillingPlan, key: string): number | null => {
    const value = p.features[key];
    if (value === null) return null;
    if (typeof value !== 'number' || !Number.isFinite(value))
      throw new Error(`Invalid ${key} for public plan ${p.id}`);
    return value;
  };
  const feature = (
    p: BillingPlan,
    key: string,
    format: (n: number) => string
  ) => {
    const value = number(p, key);
    return value === -1
      ? 'Unlimited'
      : value === null
        ? 'By agreement'
        : format(value);
  };
  const planRows = [
    row('Users included', (p) =>
      feature(p, 'max_users', (n) => (n === 1 ? '1' : `Up to ${n}`))
    ),
    row('Agents', (p) => feature(p, 'max_agents', String)),
    row('BYOK analysis quota / month', (p) =>
      feature(p, 'byok_ingest_tokens_monthly', (n) => `${compact(n)} tokens`)
    ),
    row('Built-in model allowance', (p) =>
      typeof p.features.hosted_credit_one_time_usd === 'number'
        ? `${money(p.features.hosted_credit_one_time_usd)} one-time credit`
        : feature(
            p,
            'hosted_models_monthly_limit_usd',
            (n) => `${money(n)} / month`
          )
    ),
    row('Analytics history', (p) => {
      const days = number(p, 'retention_days');
      return days === -1 || days === null
        ? 'Custom'
        : days === 183
          ? '6 months (183 days)'
          : days % 365 === 0
            ? `${days / 365} ${days === 365 ? 'year' : 'years'}`
            : `${days} days`;
    }),
    row('Extra users', (p) =>
      p.seat_addon
        ? `${money(p.seat_addon.price_per_user_monthly)}/user/mo or ${money(p.seat_addon.price_per_user_annually)}/user/yr, up to ${p.seat_addon.max_users}`
        : false
    ),
  ];
  const capabilityRows = Object.entries(CAPABILITIES)
    .filter(([key]) => cloudPlans.some((p) => p.capabilities?.includes(key)))
    .map(([key, label]) =>
      row(label, (p) => p.capabilities?.includes(key) === true)
    );
  const minimum = catalog.storage_retention?.minimum_days;
  return {
    ...pricing,
    plans: configured.map((p) => {
      const entry = plans.find((c) => c.id === p.id)!;
      const isContact = isQuoted(entry);
      const annual = entry.price_annually;
      const monthly = entry.price_monthly;
      if (
        !isContact &&
        (typeof annual !== 'number' || typeof monthly !== 'number')
      )
        throw new Error(`Public plan ${p.id} has invalid pricing`);
      const users = number(entry, 'max_users');
      return {
        ...p,
        deployment: deploymentOf(p, entry),
        name: entry.name,
        price_monthly: monthly ?? null,
        price_annually: annual ?? null,
        price_label: isContact ? p.price_label : undefined,
        // Both notes are left unset on purpose: `formatPlanPrice` derives the
        // period line from these exact numbers, so the card, the SSR markup
        // and the catalog can never state three different things.
        price_note: undefined,
        price_note_annual: undefined,
        tagline:
          entry.id === 'free'
            ? 'Start with your own provider keys.'
            : entry.id === 'pro'
              ? 'One person, unlimited agents.'
              : entry.id === 'enterprise'
                ? `Dedicated or self-hosted, up to ${users} users.`
                : `Up to ${users} people, every agent governed.`,
        cta_text: isContact
          ? 'Contact us'
          : entry.id === 'free'
            ? 'Start free'
            : `Get ${entry.name}`,
        cta_url: isContact ? '/request-demo' : '/register',
        features: [],
      };
    }),
    comparison: {
      title: 'Compare cloud plans',
      note: `BYOK analysis quotas never stop the gateway, firewall, approvals or budgets. Above the quota, analytics detail is reduced. Your provider charges on your own keys are separate and never marked up by Preloop. Analytics history controls access and storage for usage and runtime-session analytics. Older analytics are periodically removed; longer grandfathered commitments remain protected.${minimum ? ` Stored audit and evidence records are retained for at least ${minimum} days under the account policy.` : ' Stored audit and evidence records follow the account retention policy.'} Legal holds can retain records longer.`,
      groups: [
        { title: 'Plan limits', rows: planRows },
        {
          title: 'Governance in every cloud plan',
          rows: [
            'MCP firewall and model gateway',
            'Single-person approvals and basic account, agent, API-key and model budgets',
            'Cost overview and session replay',
          ].map((label) => row(label, () => true)),
        },
        ...(capabilityRows.length
          ? [{ title: 'Additional capabilities', rows: capabilityRows }]
          : []),
      ],
    },
  };
}
