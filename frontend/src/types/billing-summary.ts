export interface Plan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  features: { [key: string]: any };
}

/**
 * BYOK ingestion quota, from GET /api/v1/billing/summary (`ingestion_quota`).
 *
 * Exhausting this NEVER stops anything: the gateway keeps proxying and the
 * firewall, approvals and budgets keep enforcing. Only the detail of derived
 * analytics thins out. Any copy here that implies agents stop is a
 * product-safety bug, not a wording preference.
 */
export interface IngestionQuota {
  plan_id: string;
  quota_tokens: number;
  used_tokens: number;
  remaining_tokens: number | null;
  is_unlimited: boolean;
  over_quota: boolean;
  degraded_analytics: boolean;
  usage_ratio: number;
  approaching_limit: boolean;
  period_start: string;
  period_end: string;
}

/** Seat usage against the plan's included bracket. */
export interface SeatSummary {
  active_users: number;
  included_users: number | null;
  max_users: number | null;
  over_included: boolean;
  seat_addon: {
    price_per_user_monthly: number;
    price_per_user_annually: number;
    max_users: number;
  } | null;
}

export interface Subscription {
  plan_id: string;
  status: string;
  current_period_end: string;
}

export interface HostedModelUsageRow {
  ai_model_id: string | null;
  model_name: string;
  model_alias: string | null;
  tier: string | null;
  provider_name: string | null;
  request_count: number;
  total_tokens: number;
  estimated_cost: number;
}

export interface BillingSummary {
  subscription: Subscription | null;
  plan: Plan | null;
  /**
   * What the account is entitled to right now. An ended trial leaves a stale
   * subscription row behind, so `subscription` alone never answers "what plan
   * am I on?". Older servers omit these fields; the view falls back to the
   * subscription status and period end.
   */
  effective_plan_id?: string | null;
  effective_plan?: { id: string; name: string } | null;
  trial: {
    is_trialing: boolean;
    days: number;
    requires_payment_method: boolean;
    hosted_model_hard_cap_usd: number | null;
    is_expired?: boolean;
    ended_at?: string | null;
  };
  hosted_models: {
    billing_period_start: string;
    billing_period_end: string;
    included_limit_usd: number | null;
    active_limit_usd: number | null;
    /** null when the server has no verified spend figure, not zero spend. */
    current_usage_usd: number | null;
    remaining_limit_usd: number | null;
    /**
     * Sent by the server, deliberately not rendered: there is no way to buy
     * extra usage yet, so the price would advertise a feature that does not
     * exist. Kept here because it describes the payload.
     */
    extra_credit_price_per_usd: number;
    models: HostedModelUsageRow[];
    /** One-time credit granted to card-free free accounts. Never resets. */
    one_time_credit_usd?: number | null;
    lifetime_usage_usd?: number | null;
  };
  ingestion_quota?: IngestionQuota | null;
  seats?: SeatSummary | null;
}
