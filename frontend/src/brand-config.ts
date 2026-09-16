/**
 * Brand configuration interfaces
 *
 * These interfaces define the structure of brand-specific configuration
 * loaded from brands.yaml at build time.
 */

export interface BrandCompany {
  legal_name: string;
  address: string;
  city: string;
}

export interface BrandBranding {
  logo_light: string;
  logo_dark: string;
  favicon: string;
  primary_color: string;
  gradient_product: string;
  gradient_ai: string;
}

export interface BrandSocial {
  twitter: string;
  linkedin: string;
  instagram: string;
}

export interface BrandMeta {
  title: string;
  description: string;
  extended_description?: string;
  keywords: string;
  og_image: string;
  og_title?: string;
  og_description?: string;
}

export interface BrandHero {
  title: string;
  lead: string;
  cta_primary: string;
  cta_primary_url?: string; // Optional - if set, primary CTA links here instead of signup
  cta_secondary: string;
  cta_secondary_url: string;
  // Optional one-line install command rendered as a click-to-copy box below
  // the hero CTAs (e.g. "curl -fsSL https://preloop.ai/install/cli | sh").
  install_command?: string;
  // Optional caption shown under the install command box.
  install_caption?: string;
  // Optional tabs for the hero install widget. With two or more entries the
  // widget renders a tab per option and swaps the command/caption on click
  // (e.g. "Install the CLI" vs "Install the full stack"). The first entry is
  // the default. Takes precedence over install_command/install_caption.
  install_tabs?: Array<{ label: string; command: string; caption?: string }>;
  // Optional short credibility tags shown under the hero CTAs.
  trust_tags?: string[];
  // Optional static product shot filling the right half of the hero.
  image?: string;
  image_alt?: string;
  // Optional YouTube video/playlist URL (watch?v=...&list=... or a bare
  // playlist URL). When set together with `image`, the hero screenshot gets a
  // play-button overlay; clicking swaps the image in place for a
  // youtube-nocookie.com embed. Click-to-load: no YouTube network activity
  // happens until the visitor clicks. Absent/empty (the default) leaves the
  // hero exactly as it is today — self-hosted instances should not set this.
  video_playlist_url?: string;
}

export interface BrandFeature {
  title: string;
  text: string;
  videoUrl: string;
  placeholderImg: string;
}

export interface BrandFAQ {
  q: string;
  a: string;
}

export interface BrandGetStartedFeature {
  icon: string;
  title: string;
  text: string;
}

export interface BrandMCPConfig {
  ide: string;
  ide_name: string;
  logo_path: string;
  logo_width: string;
  prerequisites: string[];
  setup_instructions: string;
  code: string;
}

export interface BrandGetStarted {
  title: string;
  link_text: string;
  link_url: string;
  features: BrandGetStartedFeature[];
  mcp_setup_title: string;
  mcp_configs: BrandMCPConfig[];
}

export interface PricingPlan {
  id: string;
  name: string;
  price_monthly: number | null;
  price_annually: number | null;
  price_label?: string;
  /**
   * Secondary price line shown under the headline number, e.g. the annual
   * equivalent of a monthly price. Free-form so a plan can say "billed
   * annually" or "2 months free" without the card inventing arithmetic.
   */
  price_note?: string;
  /** Same as `price_note` but shown when the annual interval is selected. */
  price_note_annual?: string;
  badge?: string;
  highlight?: boolean;
  cta_text?: string;
  cta_url?: string;
  description?: string;
  /**
   * The single line printed on the card. The approved card shape is one
   * number plus one line: quotas, retention, and the feature split live in
   * the comparison table below the fold, never on the card.
   */
  tagline?: string;
  /**
   * Which pricing tab the plan belongs to. `cloud` plans are the hosted
   * subscriptions shown with the billing period toggle and the comparison
   * table; `dedicated` plans are quoted (self-managed or dedicated) and
   * belong on the Dedicated tab. An explicit value is
   * honoured so EE brands.yaml can route a plan without a catalog change.
   * When unset, a configured `catalog_path` tags the plan from the billing
   * catalog; otherwise the tab defaults to `cloud`.
   */
  deployment?: 'cloud' | 'dedicated';
  features: string[];
}

export interface PricingFAQ {
  q: string;
  a: string;
}

/**
 * One row of the below-the-fold comparison table. `values` is keyed by plan
 * id; a missing key renders as an empty cell rather than a false claim.
 * Values are either free text ("Up to 5", "500M tokens") or the booleans
 * `true`/`false` which render as an included/excluded mark.
 */
export interface PricingComparisonRow {
  label: string;
  values: Record<string, string | boolean>;
}

export interface PricingComparisonGroup {
  title: string;
  rows: PricingComparisonRow[];
}

export interface PricingComparison {
  title?: string;
  note?: string;
  groups: PricingComparisonGroup[];
}

/**
 * The Dedicated tab: self-managed and quoted editions.
 *
 * These are not subscriptions in `plans.yaml`, so there is no catalog to
 * generate them from and the brand states them directly. The shape is
 * deliberately the same as the Cloud tab (cards plus one comparison table) so
 * both tabs render through exactly the same code and cannot drift into two
 * different layouts. Only the columns differ.
 *
 * No billing period applies here: an open-source edition is free and a quoted
 * edition is agreed per year, so these plans carry a `price_label` rather
 * than a monthly/annual pair.
 *
 * Replaces the old `deployment_options` list. That key is no longer read
 * (unknown keys are ignored), so a leftover block renders nothing. Author
 * this block in brands.yaml; the EE Preloop brand already ships it.
 */
export interface PricingDedicated {
  /** Tab and section label. Defaults to "Dedicated". */
  label?: string;
  /** Optional one-line intro under the section heading. */
  lead?: string;
  plans: PricingPlan[];
  comparison?: PricingComparison;
}

export interface PricingConfig {
  /** Optional catalog path relative to brands.yaml; required for EE cloud pricing. */
  catalog_path?: string;
  enabled?: boolean;
  title?: string;
  lead?: string;
  /** Tab and section label for the hosted ladder. Defaults to "Cloud". */
  cloud_label?: string;
  billing_toggle?: boolean;
  plans: PricingPlan[];
  comparison?: PricingComparison;
  /**
   * Dedicated tab (cards plus one comparison table). Replaces
   * `deployment_options`. Omit only for a cloud-only brand; a leftover
   * `deployment_options` key is ignored and will not render.
   */
  dedicated?: PricingDedicated;
  faqs?: PricingFAQ[];
}

export interface BrandLanding {
  meta: BrandMeta;
  features_layout: 'carousel' | 'grid';
  hero: BrandHero;
  features: BrandFeature[];
  faqs: BrandFAQ[];
  legal_disclaimer?: string;
  get_started: BrandGetStarted;
  pricing?: PricingConfig;
}

/**
 * Edition type - determines UI behavior, NOT feature set
 *
 * - 'saas': Full marketing landing page, pricing page, signup CTAs
 * - 'selfhosted': Minimal UI, redirects to login, no public pricing
 *
 * Enterprise features (RBAC, audit, etc.) are controlled by backend
 * plugins and the /api/v1/features endpoint, not by edition.
 *
 * Enterprise self-hosted deployments use edition: 'selfhosted'
 * with the enterprise Docker image for full plugin support.
 */
export type BrandEdition = 'saas' | 'selfhosted';

/** A named-instrument regulation page that shipped for this brand. */
export interface RegulationNavLink {
  href: string;
  label: string;
}

// Runtime config - minimal metadata injected into window.BRAND_CONFIG
export interface BrandRuntimeConfig {
  name: string;
  domain: string;
  edition: BrandEdition; // 'saas' = full marketing site, 'selfhosted' = minimal login-focused
  company: BrandCompany;
  branding: BrandBranding;
  social: BrandSocial;
  /**
   * Regulation pages discovered at build time (markdown file present plus a
   * REGULATION_PAGE_META entry). Absent on older builds, so treat it as
   * optional and render nothing when it is missing.
   */
  regulation_pages?: RegulationNavLink[];
  /**
   * Public markdown pages discovered at build time from
   * `content/<brand>/*.md` and `content/<brand>/resources/*.md`. EE adds
   * routes by dropping files; OSS never lists pages it does not ship.
   */
  static_markdown_pages?: Array<{ path: string; src: string }>;
  /**
   * Optional legal disclaimer from the landing content knob. The shared
   * footer renders it above the copyright row when this is set.
   */
  legal_disclaimer?: string;
}

// Full config - used at build time only (includes landing content)
export interface BrandConfig extends BrandRuntimeConfig {
  landing: BrandLanding;
}

export interface BrandsConfig {
  brands: {
    [key: string]: BrandConfig;
  };
}

/**
 * Get the current brand configuration
 *
 * This function retrieves the brand config that was injected into
 * window.BRAND_CONFIG by the Vite plugin at build time.
 *
 * @returns The current brand configuration
 * @throws Error if BRAND_CONFIG is not defined
 */
export function getBrandConfig(): BrandRuntimeConfig {
  if (typeof window === 'undefined') {
    throw new Error('getBrandConfig() can only be called in the browser');
  }

  const config = (window as any).BRAND_CONFIG as BrandRuntimeConfig | undefined;

  if (!config) {
    throw new Error(
      'BRAND_CONFIG not found on window. Make sure the Vite brand plugin is configured correctly.'
    );
  }

  return config;
}

/**
 * Check if BRAND_CONFIG is available
 *
 * Useful for defensive programming when the config might not be loaded yet.
 *
 * @returns true if BRAND_CONFIG is available
 */
export function hasBrandConfig(): boolean {
  return typeof window !== 'undefined' && !!(window as any).BRAND_CONFIG;
}

/**
 * Check if the current brand is self-hosted edition
 * Self-hosted editions have minimal landing pages and no pricing
 */
export function isSelfHosted(): boolean {
  try {
    return getBrandConfig().edition === 'selfhosted';
  } catch {
    return false;
  }
}

/**
 * Check if the current brand is SaaS edition
 * SaaS editions have full marketing landing pages and pricing
 */
export function isSaaS(): boolean {
  try {
    return getBrandConfig().edition === 'saas';
  } catch {
    return true; // Default to SaaS behavior
  }
}
