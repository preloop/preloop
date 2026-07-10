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
  badge?: string;
  highlight?: boolean;
  cta_text?: string;
  cta_url?: string;
  description?: string;
  features: string[];
}

export interface PricingFAQ {
  q: string;
  a: string;
}

export interface PricingConfig {
  enabled?: boolean;
  title?: string;
  lead?: string;
  billing_toggle?: boolean;
  plans: PricingPlan[];
  faqs?: PricingFAQ[];
}

export interface BrandLanding {
  meta: BrandMeta;
  features_layout: 'carousel' | 'grid';
  hero: BrandHero;
  features: BrandFeature[];
  faqs: BrandFAQ[];
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

// Runtime config - minimal metadata injected into window.BRAND_CONFIG
export interface BrandRuntimeConfig {
  name: string;
  domain: string;
  edition: BrandEdition; // 'saas' = full marketing site, 'selfhosted' = minimal login-focused
  company: BrandCompany;
  branding: BrandBranding;
  social: BrandSocial;
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
