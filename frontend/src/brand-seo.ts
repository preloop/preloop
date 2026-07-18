import type { BrandConfig, PricingPlan } from './brand-config';
import {
  BLOG_BASE_PATH,
  buildBlogBreadcrumbSchema,
  buildBlogPostingSchema,
  buildBlogSchema,
  get_blog_index_meta,
  get_blog_post_meta,
  get_blog_routes,
  get_blog_slug_from_route,
  type BlogPost,
} from './blog-seo';

export type RouteMeta = {
  title: string;
  description: string;
  keywords: string;
  og_image: string;
  og_title: string;
  og_description: string;
};

/**
 * Per-slug metadata for competitor comparison landing pages served at
 * `/vs/<slug>`. Adding a new comparison page is a two-step change:
 *
 *   1. Drop a markdown file at `<brand>/vs/<slug>.md` in the content dir.
 *   2. Add an entry here with the slug's SEO metadata.
 *
 * The comparison competitor's name and the core keyword set live here so
 * they can be tuned independently of the body copy.
 */
export type VsPageMeta = {
  /** Display name of the competitor (e.g. "AWS Bedrock AgentCore"). */
  competitor: string;
  /** Page `<title>` element. */
  title: string;
  /** Meta description (< ~160 chars recommended). */
  description: string;
  /** Additional keywords specific to this comparison. */
  keywords: string;
  /** Optional Open Graph title (defaults to `title`). */
  og_title?: string;
  /** Optional Open Graph description (defaults to `description`). */
  og_description?: string;
};

export const VS_PAGE_META: Record<string, VsPageMeta> = {
  'aws-agentcore': {
    competitor: 'AWS Bedrock AgentCore',
    title: 'Preloop vs AWS Bedrock AgentCore — Open-Source Alternative',
    description:
      'Open source AWS AgentCore alternative: self-hosted agent control plane with MCP firewall, AI model gateway, human approvals, runtime observability, and audit trail. No AWS lock-in.',
    keywords:
      'open source AWS AgentCore alternative, AWS Bedrock AgentCore alternative, self-hosted agent control plane, MCP firewall without AWS lock-in, open source AI agent governance, AI agent control plane, MCP gateway self-hosted, AI model gateway, AI agent audit trail, Claude Code governance, Codex CLI governance, Cursor agent security',
  },
  mintmcp: {
    competitor: 'MintMCP',
    title: 'Preloop vs MintMCP — Open-Source MCP Gateway with Approvals',
    description:
      'Open-source MintMCP alternative: self-hosted MCP gateway with HITL approvals, AI model gateway with budgets, runtime observability, and audit trail. One control plane for agents.',
    keywords:
      'open-source MintMCP alternative, self-hosted MCP gateway, HITL approvals, MCP firewall, MCP gateway with approvals, AI agent control plane, AI model gateway, AI agent audit trail, Claude Code governance, Cursor agent security, Codex CLI governance',
  },
  portkey: {
    competitor: 'Portkey',
    title: 'Preloop vs Portkey — Agent Control Plane or AI Gateway?',
    description:
      'Open-source Portkey alternative: AI gateway plus MCP firewall, human approvals, runtime observability, and audit. Self-hosted LLM proxy with agent-level governance.',
    keywords:
      'open-source Portkey alternative, AI gateway plus MCP firewall, self-hosted LLM proxy, AI agent control plane, AI model gateway open source, AI agent governance, MCP firewall, Claude Code governance, Codex CLI governance, Cursor agent security, AI agent observability',
  },
  zenity: {
    competitor: 'Zenity',
    title: 'Preloop vs Zenity — Developer-First AI Agent Governance',
    description:
      'Open-source Zenity alternative: agent governance for engineers with MCP-native policy enforcement, human approvals, runtime observability, and audit trail. Self-hostable and developer-first.',
    keywords:
      'open-source Zenity alternative, agent governance for engineers, MCP-native policy enforcement, AI agent control plane, AI agent security, AI agent governance, MCP firewall, Claude Code governance, Codex CLI governance, Cursor agent security, AI agent audit trail, AI agent runtime security',
  },
  litellm: {
    competitor: 'LiteLLM',
    title: 'Preloop vs LiteLLM — Agent Control Plane vs AI Gateway',
    description:
      'LiteLLM AI Gateway alternative: model gateway plus MCP firewall, human approvals, session observability, cost reconciliation, and audit. LiteLLM governs model traffic; Preloop governs agents.',
    keywords:
      'LiteLLM alternative, LiteLLM AI gateway alternative, LLM gateway with approvals, open-source LLM proxy with policies, AI model gateway open source, AI agent control plane, MCP firewall, MCP gateway with approvals, AI agent governance, AI cost reconciliation, Claude Code governance, Codex CLI governance, Cursor agent security, AI agent audit trail',
  },
  runlayer: {
    competitor: 'Runlayer',
    title: 'Preloop vs Runlayer — Open-Source MCP Security & Approvals',
    description:
      'Open-source Runlayer alternative: self-hostable MCP firewall with per-call human approvals in the free core, AI model gateway, runtime observability, and a tamper-evident audit trail. Apache 2.0, no vendor lock-in.',
    keywords:
      'open-source Runlayer alternative, self-hosted MCP security, MCP firewall with human approvals, per-call agent approvals, AI agent control plane, MCP gateway open source, AI agent governance, AI agent audit trail, Claude Code governance, Codex CLI governance, Cursor agent security',
  },
  lunar: {
    competitor: 'Lunar.dev MCPX',
    title: 'Preloop vs Lunar.dev MCPX — Approvals in the Free Core',
    description:
      'Open-source Lunar.dev MCPX alternative: MCP firewall with human-in-the-loop approvals in the free core, AI model gateway, runtime observability, and audit. Self-hostable Apache 2.0 control plane.',
    keywords:
      'Lunar.dev MCPX alternative, open-source MCP gateway, MCP firewall with approvals, human in the loop agent approvals, self-hosted MCP gateway, AI agent control plane, AI model gateway, AI agent governance, Claude Code governance, Codex CLI governance, Cursor agent security',
  },
  helicone: {
    competitor: 'Helicone',
    title: 'Preloop vs Helicone — Migrate to an Active Control Plane',
    description:
      'Migrating from Helicone? Preloop is an actively developed, Apache 2.0 AI agent control plane: an AI model gateway plus an MCP firewall, human approvals, runtime observability, and audit that Helicone does not have.',
    keywords:
      'Helicone alternative, Helicone migration, open-source LLM gateway, AI model gateway with approvals, MCP firewall, AI agent control plane, LLM observability alternative, AI agent governance, Claude Code governance, Codex CLI governance, Cursor agent security',
  },
};

export function get_vs_slug_from_route(route: string): string | null {
  if (!route.startsWith('/vs/')) {
    return null;
  }
  const slug = route.slice('/vs/'.length);
  return slug && !slug.includes('/') ? slug : null;
}

export function get_vs_slugs(): string[] {
  return Object.keys(VS_PAGE_META);
}

export function get_route_from_filename(filename: string): string {
  if (filename.includes('ai-act-readiness.html')) {
    return '/ai-act-readiness';
  }

  if (filename.includes('about.html')) {
    return '/about';
  }

  if (filename.includes('whatis-mcp.html')) {
    return '/whatis-mcp';
  }

  if (filename.includes('terms.html')) {
    return '/terms';
  }

  if (filename.includes('privacy.html')) {
    return '/privacy';
  }

  if (filename.includes('pricing.html')) {
    return '/pricing';
  }

  // Match any `/vs/<slug>.html` file (competitor comparison landing pages).
  const vsMatch = filename.match(/vs[\\/]([A-Za-z0-9_-]+)\.html$/);
  if (vsMatch) {
    return `/vs/${vsMatch[1]}`;
  }

  // Match any `/resources/<slug>.html` file (long-form pillar articles).
  const resourcesMatch = filename.match(
    /resources[\\/]([A-Za-z0-9_-]+)\.html$/
  );
  if (resourcesMatch) {
    return `/resources/${resourcesMatch[1]}`;
  }

  // The blog index is written to `blog/index.html`; posts to
  // `blog/<slug>.html`. Check the index first so it is not mistaken for a
  // post with the slug "index".
  if (/blog[\\/]index\.html$/.test(filename)) {
    return BLOG_BASE_PATH;
  }
  const blogMatch = filename.match(/blog[\\/]([a-z0-9][a-z0-9-]*)\.html$/);
  if (blogMatch) {
    return `${BLOG_BASE_PATH}/${blogMatch[1]}`;
  }

  return '/';
}

export function get_canonical_url(route: string, config: BrandConfig): string {
  return `https://${config.domain}${route}`;
}

export function get_meta_for_route(
  route: string,
  config: BrandConfig,
  posts: BlogPost[] = []
): RouteMeta {
  const meta = config.landing?.meta || {};
  const default_title = meta.title || config.name || 'Preloop';
  const default_description = meta.description || '';
  const default_keywords = meta.keywords || '';
  const default_og_image = meta.og_image || '';
  const default_og_title = meta.og_title || default_title;
  const default_og_description = meta.og_description || default_description;

  switch (route) {
    case '/':
      return {
        title: default_title,
        description: default_description,
        keywords: default_keywords,
        og_image: default_og_image,
        og_title: default_og_title,
        og_description: default_og_description,
      };
    case '/privacy':
      return {
        title: `Privacy Policy - ${config.name}`,
        description: `${config.name} Privacy Policy - Learn how we protect your data.`,
        keywords: `${config.name}, Privacy Policy, Data Protection`,
        og_image: default_og_image,
        og_title: `Privacy Policy - ${config.name}`,
        og_description: `${config.name} Privacy Policy - Learn how we protect your data.`,
      };
    case '/pricing':
      return {
        title: `Pricing — Open-Source AI Agent Control Plane | ${config.name}`,
        description: `Run ${config.name} yourself for free under Apache 2.0, or let us host the MCP firewall, AI model gateway, and human approvals for your team. Compare Open Source, Teams, and Enterprise plans.`,
        keywords: `${config.name} pricing, open-source AI agent control plane pricing, MCP firewall pricing, AI model gateway pricing, self-hosted AI agent governance, Apache 2.0 AI agent platform, Teams plan, Enterprise plan, AWS Bedrock AgentCore alternative pricing`,
        og_image: default_og_image,
        og_title: `Pricing — Open-Source AI Agent Control Plane | ${config.name}`,
        og_description: `Run ${config.name} yourself for free under Apache 2.0, or let us host the MCP firewall, AI model gateway, and human approvals for your team.`,
      };
    case '/terms':
      return {
        title: `Terms of Service - ${config.name}`,
        description: `${config.name} Terms of Service - Read our terms and conditions.`,
        keywords: `${config.name}, Terms of Service, Legal`,
        og_image: default_og_image,
        og_title: `Terms of Service - ${config.name}`,
        og_description: `${config.name} Terms of Service - Read our terms and conditions.`,
      };
    case '/whatis-mcp':
      return {
        title: `What is MCP? - ${config.name}`,
        description: `Learn what the Model Context Protocol (MCP) is and how ${config.name} uses it to govern AI agents and tool access.`,
        keywords: `${config.name}, MCP, Model Context Protocol, AI agents, AI governance`,
        og_image: default_og_image,
        og_title: `What is MCP? - ${config.name}`,
        og_description: `Learn what the Model Context Protocol (MCP) is and how ${config.name} uses it to govern AI agents and tool access.`,
      };
    case '/about':
      return {
        title: `About - ${config.name}`,
        description: `Learn about ${config.name} and our mission to make AI automation governable, observable, and human-centered.`,
        keywords: `${config.name}, About, Company, Team, Mission, AI governance`,
        og_image: default_og_image,
        og_title: `About - ${config.name}`,
        og_description: `Learn about ${config.name} and our mission to make AI automation governable, observable, and human-centered.`,
      };
    case '/ai-act-readiness':
      return {
        title: `EU AI Act readiness with ${config.name}`,
        description: `${config.name} helps teams implement approvals, runtime visibility, policy enforcement, and audit trails that can support AI governance programs and EU AI Act readiness work.`,
        keywords: `${config.name}, EU AI Act, AI Act readiness, AI governance, AI approvals, AI audit trail, operational AI governance`,
        og_image: default_og_image,
        og_title: `EU AI Act readiness with ${config.name}`,
        og_description: `${config.name} supports AI governance programs with approvals, policy enforcement, runtime visibility, and audit trails for AI Act readiness work.`,
      };
    case '/resources/ai-agent-control-plane-2026':
      return {
        title: `The AI Agent Control Plane in 2026 — MCP Gateways, Model Gateways, and Human Approvals | ${config.name}`,
        description: `The definitive 2026 guide to AI agent control planes: the five layers (MCP firewall, AI gateway, approvals, observability, audit), key vendors (AWS AgentCore, Portkey, MintMCP, Zenity, LiteLLM, ${config.name}), and how to pick the right architecture.`,
        keywords: `AI agent control plane, MCP gateway vs AI gateway, AWS Bedrock AgentCore alternative, best MCP firewall, AI agent governance 2026, LLM gateway comparison, human in the loop AI agent, AI Act readiness, MCP firewall, AI model gateway, AgentOps, ${config.name}`,
        og_image: '/assets/mcp-firewall.svg',
        og_title: `The AI Agent Control Plane in 2026: MCP Gateways, Model Gateways, and Human Approvals Explained`,
        og_description: `A neutral reference on AI agent control planes — the five layers, the vendors shaping them (AWS AgentCore, MintMCP, Lunar.dev MCPX, Portkey, Helicone, LiteLLM, Zenity, Lakera, ${config.name}, and more), and how platform and security teams pick an approach.`,
      };
    case BLOG_BASE_PATH:
      return get_blog_index_meta(config);
    default: {
      // Blog posts at `/blog/<slug>`. Metadata comes from the post's own
      // frontmatter, so an unknown slug falls through to the site defaults
      // rather than inventing a title.
      const blog_slug = get_blog_slug_from_route(route);
      if (blog_slug) {
        const post = posts.find((candidate) => candidate.slug === blog_slug);
        if (post) {
          return get_blog_post_meta(post, config);
        }
      }

      // Competitor comparison landing pages at `/vs/<slug>`.
      const vs_slug = get_vs_slug_from_route(route);
      if (vs_slug && VS_PAGE_META[vs_slug]) {
        const vs = VS_PAGE_META[vs_slug];
        const merged_keywords = [vs.keywords, default_keywords]
          .filter((value) => value && value.trim().length > 0)
          .join(', ');

        return {
          title: vs.title,
          description: vs.description,
          keywords: merged_keywords,
          og_image: default_og_image,
          og_title: vs.og_title || vs.title,
          og_description: vs.og_description || vs.description,
        };
      }

      return {
        title: default_title,
        description: default_description,
        keywords: default_keywords,
        og_image: default_og_image,
        og_title: default_og_title,
        og_description: default_og_description,
      };
    }
  }
}

export function get_static_routes(config: BrandConfig): string[] {
  return get_static_routes_with_options(config, true);
}

export function get_static_routes_with_options(
  config: BrandConfig,
  include_ai_act_readiness: boolean,
  vs_slugs: string[] = [],
  posts: BlogPost[] = []
): string[] {
  const routes = ['/', '/privacy', '/terms', '/whatis-mcp'];

  if ((config.edition || 'saas') === 'saas') {
    routes.push('/pricing', '/about');
    if (include_ai_act_readiness) {
      routes.push('/ai-act-readiness');
    }
    // Include one /vs/<slug> entry per comparison page that has both a
    // metadata registration and a markdown file on disk. The list is
    // supplied by the caller so SEO helpers stay pure and the vite plugin
    // controls which slugs actually ship.
    for (const slug of vs_slugs) {
      if (VS_PAGE_META[slug]) {
        routes.push(`/vs/${slug}`);
      }
    }
    // Long-form pillar resources. Listed explicitly here so the sitemap and
    // llms.txt stay in sync with the pages actually generated by the build.
    routes.push('/resources/ai-agent-control-plane-2026');

    // Blog index plus one entry per published post. Supplied by the caller
    // for the same reason as `vs_slugs`: these helpers stay pure and the
    // vite plugin decides what actually ships.
    routes.push(...get_blog_routes(config, posts));
  }

  return routes;
}

// -----------------------------------------------------------------------------
// Structured-data (JSON-LD) builders
// -----------------------------------------------------------------------------
//
// Each builder is a pure function that takes a BrandConfig (plus optional
// contextual arguments) and returns a schema.org object. The public entry point
// `get_structured_data_for_route` composes an array of these objects per route
// so the rendered <script id="preloop-structured-data"> tag contains a single
// JSON array — the recommended shape for multi-type JSON-LD documents.
//
// All public URLs are generated with `get_canonical_url` / the brand domain so
// the schema stays in sync with `brands.yaml`. Image URLs are always absolute.

type SchemaObject = Record<string, unknown>;

const ORGANIZATION_ID_FRAGMENT = '#organization';
const WEBSITE_ID_FRAGMENT = '#website';

function get_origin(config: BrandConfig): string {
  return `https://${config.domain}`;
}

function absolute_url(path: string, config: BrandConfig): string {
  if (!path) {
    return '';
  }
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  const origin = get_origin(config);
  return `${origin}${path.startsWith('/') ? '' : '/'}${path}`;
}

function strip_html(input: string): string {
  if (!input) {
    return '';
  }
  let text = input;
  let prev = '';
  while (text !== prev) {
    prev = text;
    text = text.replace(/<[^>]*>/g, '');
  }
  text = text.replace(/javascript:/gi, '');
  text = text.replace(/vbscript:/gi, '');
  text = text.replace(/data:/gi, '');
  text = text.replace(/</g, '');
  return text.trim();
}

function get_organization_id(config: BrandConfig): string {
  return `${get_origin(config)}/${ORGANIZATION_ID_FRAGMENT}`;
}

function get_website_id(config: BrandConfig): string {
  return `${get_origin(config)}/${WEBSITE_ID_FRAGMENT}`;
}

function coerce_same_as(config: BrandConfig): string[] {
  const social = config.social || ({} as BrandConfig['social']);
  const entries = [
    'https://github.com/preloop/preloop',
    social?.linkedin,
    social?.twitter ? twitter_handle_to_url(social.twitter) : '',
    social?.instagram,
  ];
  return entries
    .map((entry) => (entry || '').trim())
    .filter((entry) => entry.length > 0);
}

function twitter_handle_to_url(handle: string): string {
  if (!handle) {
    return '';
  }
  if (/^https?:\/\//i.test(handle)) {
    return handle;
  }
  const name = handle.replace(/^@/, '').trim();
  return name ? `https://twitter.com/${name}` : '';
}

function build_postal_address(config: BrandConfig): SchemaObject | undefined {
  const company = config.company;
  if (!company) {
    return undefined;
  }

  const street = company.address || '';
  const city_raw = company.city || '';

  // `company.city` is stored as "San Francisco, CA 94108" — split it into the
  // schema.org fields so LLMs can consume addressLocality/region/postalCode.
  let locality = city_raw;
  let region = '';
  let postal_code = '';
  const city_match = city_raw.match(
    /^\s*([^,]+?)\s*,\s*([A-Za-z]{2})\s+([A-Za-z0-9 -]+)\s*$/
  );
  if (city_match) {
    locality = city_match[1];
    region = city_match[2];
    postal_code = city_match[3];
  }

  const address: SchemaObject = {
    '@type': 'PostalAddress',
    streetAddress: street,
    addressLocality: locality,
    addressCountry: 'US',
  };
  if (region) {
    address.addressRegion = region;
  }
  if (postal_code) {
    address.postalCode = postal_code;
  }
  return address;
}

export function buildOrganizationSchema(config: BrandConfig): SchemaObject {
  const meta = config.landing?.meta || {};
  const origin = get_origin(config);
  const logo_path = config.branding?.logo_dark || config.branding?.logo_light;

  const schema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': 'Organization',
    '@id': get_organization_id(config),
    name: config.name,
    url: `${origin}/`,
    description: meta.description || '',
    sameAs: coerce_same_as(config),
  };

  if (config.company?.legal_name) {
    schema.legalName = config.company.legal_name;
  }
  if (logo_path) {
    schema.logo = absolute_url(logo_path, config);
  }

  // The founding year is a fact from the repo — keep it in sync with the
  // company establishment year in `brand-config.ts` / AGENTS docs.
  schema.foundingDate = '2025';

  const address = build_postal_address(config);
  if (address) {
    schema.address = address;
  }

  return schema;
}

export function buildWebSiteSchema(config: BrandConfig): SchemaObject {
  const origin = get_origin(config);
  const meta = config.landing?.meta || {};
  return {
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    '@id': get_website_id(config),
    url: `${origin}/`,
    name: config.name,
    description: meta.description || '',
    publisher: { '@id': get_organization_id(config) },
  };
}

function build_offers_from_pricing_plans(
  plans: PricingPlan[],
  config: BrandConfig
): SchemaObject[] {
  return plans.map((plan) => {
    const offer: SchemaObject = {
      '@type': 'Offer',
      name: plan.name,
      url: `${get_origin(config)}/pricing`,
    };
    if (plan.description) {
      offer.description = plan.description;
    }

    if (typeof plan.price_monthly === 'number' && plan.price_monthly > 0) {
      offer.price = String(plan.price_monthly);
      offer.priceCurrency = 'USD';
      offer.category = 'Subscription';
      if (plan.price_label) {
        offer.description = offer.description
          ? `${offer.description} (${plan.price_label})`
          : plan.price_label;
      }
    } else if (plan.price_monthly === 0) {
      offer.price = '0';
      offer.priceCurrency = 'USD';
      offer.category = 'Free';
    } else {
      // Null price = "Contact sales"-style plan.
      offer.priceSpecification = {
        '@type': 'PriceSpecification',
        price: '0',
        priceCurrency: 'USD',
      };
      offer.category = 'Custom';
      if (!offer.description) {
        offer.description = 'Contact sales for pricing';
      }
    }

    return offer;
  });
}

function default_software_application_offers(
  config: BrandConfig
): SchemaObject[] {
  const pricing_url = `${get_origin(config)}/pricing`;
  return [
    {
      '@type': 'Offer',
      name: 'Open Source',
      price: '0',
      priceCurrency: 'USD',
      category: 'Free',
      url: pricing_url,
    },
    {
      '@type': 'Offer',
      name: 'Teams',
      price: '29',
      priceCurrency: 'USD',
      category: 'Subscription',
      description: 'Per user, monthly',
      url: pricing_url,
    },
    {
      '@type': 'Offer',
      name: 'Enterprise',
      priceSpecification: {
        '@type': 'PriceSpecification',
        price: '0',
        priceCurrency: 'USD',
      },
      category: 'Custom',
      description: 'Contact sales for pricing',
      url: pricing_url,
    },
  ];
}

export function buildSoftwareApplicationSchema(
  config: BrandConfig
): SchemaObject {
  const meta = config.landing?.meta || {};
  const origin = get_origin(config);
  const plans = config.landing?.pricing?.plans;

  const offers =
    plans && plans.length > 0
      ? build_offers_from_pricing_plans(plans, config)
      : default_software_application_offers(config);

  const schema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: config.name,
    alternateName: ['Preloop AI Agent Control Plane', 'Preloop MCP Firewall'],
    applicationCategory: 'DeveloperApplication',
    applicationSubCategory: 'AI Agent Governance',
    operatingSystem: 'Linux, macOS, Windows, Docker, Kubernetes',
    description: meta.description || '',
    url: `${origin}/`,
    downloadUrl: 'https://github.com/preloop/preloop',
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    isAccessibleForFree: true,
    offers,
    featureList: [
      'MCP firewall for tool access control',
      'AI model gateway with budgets and attribution (OpenAI and Anthropic compatible)',
      'Human-in-the-loop approvals (mobile, watch, Slack, Mattermost, email, webhook)',
      'Policy-as-code in YAML with CEL expressions',
      'Runtime session observability',
      'Audit trail for security and compliance',
      "Zero-touch onboarding of existing agents via 'preloop agents discover'",
      'Works with Claude Code, Codex CLI, Cursor, Gemini CLI, Hermes, OpenClaw, OpenCode, and any MCP-compatible agent',
      'Self-hostable (Docker, Kubernetes)',
      'Apache 2.0 open source license',
    ],
    keywords: meta.keywords || '',
    creator: { '@id': get_organization_id(config) },
    softwareRequirements:
      'Python 3.11+, PostgreSQL 14+ with PGVector, Docker (optional)',
  };

  // TODO(seo): add `aggregateRating` here once real user-review data is
  // available (for example from G2, Capterra, or verified first-party
  // reviews). Per Google's structured-data policy, aggregate ratings must
  // correspond to genuine reviews and must not be fabricated.

  return schema;
}

export function buildFAQPageSchema(
  config: BrandConfig
): SchemaObject | undefined {
  const faqs = config.landing?.faqs || [];
  if (faqs.length === 0) {
    return undefined;
  }
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: faqs.map((faq) => ({
      '@type': 'Question',
      name: faq.q,
      acceptedAnswer: {
        '@type': 'Answer',
        text: strip_html(faq.a),
      },
    })),
  };
}

export function buildAboutPageSchema(config: BrandConfig): SchemaObject {
  const meta = get_meta_for_route('/about', config);
  return {
    '@context': 'https://schema.org',
    '@type': 'AboutPage',
    name: meta.title,
    url: get_canonical_url('/about', config),
    description: meta.description,
    mainEntity: { '@id': get_organization_id(config) },
  };
}

export function buildWebPageSchema(
  route: string,
  config: BrandConfig
): SchemaObject {
  const meta = get_meta_for_route(route, config);
  return {
    '@context': 'https://schema.org',
    '@type': 'WebPage',
    name: meta.title,
    url: get_canonical_url(route, config),
    description: meta.description,
    isPartOf: { '@id': get_website_id(config) },
  };
}

export function buildProductSchema(config: BrandConfig): SchemaObject {
  const meta = config.landing?.meta || {};
  const plans = config.landing?.pricing?.plans;
  const offers =
    plans && plans.length > 0
      ? build_offers_from_pricing_plans(plans, config)
      : default_software_application_offers(config);

  const schema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': 'Product',
    name: config.name,
    description: meta.description || '',
    url: get_canonical_url('/pricing', config),
    brand: { '@id': get_organization_id(config) },
    offers,
  };

  const logo_path = config.branding?.logo_dark || config.branding?.logo_light;
  if (logo_path) {
    schema.image = absolute_url(logo_path, config);
  }

  return schema;
}

export function buildArticleSchema(
  config: BrandConfig,
  route: string,
  headline: string,
  description: string,
  options: {
    date_published?: string;
    about?: string[];
  } = {}
): SchemaObject {
  const meta = config.landing?.meta || {};
  const og_image = meta.og_image
    ? absolute_url(meta.og_image, config)
    : undefined;
  const date_published =
    options.date_published || new Date().toISOString().slice(0, 10);

  const schema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': 'Article',
    headline,
    description,
    url: get_canonical_url(route, config),
    datePublished: date_published,
    author: { '@id': get_organization_id(config) },
    publisher: { '@id': get_organization_id(config) },
    isAccessibleForFree: true,
  };

  if (options.about && options.about.length > 0) {
    schema.about = options.about;
  }
  if (og_image) {
    schema.image = og_image;
  }
  return schema;
}

function build_vs_article_schema(
  config: BrandConfig,
  route: string
): SchemaObject {
  const slug = route.replace(/^\/vs\//, '').replace(/\/$/, '');
  const registered = slug ? VS_PAGE_META[slug] : undefined;

  // Prefer registered metadata (matches page <title> and meta description).
  // Fall back to title-cased slug so schema still renders for routes that were
  // created ad-hoc without a VS_PAGE_META entry.
  const competitor =
    registered?.competitor ||
    slug
      .split(/[-_]/)
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');

  const meta = get_meta_for_route(route, config);
  const headline = registered
    ? meta.title
    : competitor
      ? `${config.name} vs ${competitor}`
      : `${config.name} comparison`;
  const description = registered
    ? meta.description
    : competitor
      ? `How ${config.name} compares to ${competitor} as an AI agent control plane, MCP firewall, and AI model gateway.`
      : `How ${config.name} compares to other AI agent governance tools.`;

  const schema = buildArticleSchema(config, route, headline, description, {
    about: competitor
      ? [config.name, competitor, 'AI agent control plane', 'MCP firewall']
      : [config.name, 'AI agent governance'],
  });
  if (competitor) {
    (schema as SchemaObject).mentions = [
      { '@type': 'Thing', name: competitor },
      { '@type': 'Thing', name: config.name },
    ];
  }
  return schema;
}

export function get_structured_data_for_route(
  route: string,
  config: BrandConfig,
  posts: BlogPost[] = []
): SchemaObject[] {
  const organization = buildOrganizationSchema(config);
  const website = buildWebSiteSchema(config);

  if (route === BLOG_BASE_PATH) {
    return [organization, website, buildBlogSchema(config, posts)];
  }

  // Blog posts emit BlogPosting + BreadcrumbList. Structured data carries
  // disproportionate weight with answer engines, which is why posts get a
  // real `Person` author, both dates, and breadcrumbs rather than the generic
  // WebPage fallback.
  const blog_slug = get_blog_slug_from_route(route);
  if (blog_slug) {
    const post = posts.find((candidate) => candidate.slug === blog_slug);
    if (post) {
      return [
        organization,
        website,
        buildBlogPostingSchema(post, config),
        buildBlogBreadcrumbSchema(post, config),
      ];
    }
  }

  if (route === '/') {
    const software_app = buildSoftwareApplicationSchema(config);
    const faq_page = buildFAQPageSchema(config);
    return [
      organization,
      website,
      software_app,
      ...(faq_page ? [faq_page] : []),
    ];
  }

  if (route === '/about') {
    return [organization, website, buildAboutPageSchema(config)];
  }

  if (route === '/pricing') {
    return [organization, website, buildProductSchema(config)];
  }

  if (route === '/whatis-mcp') {
    const meta = get_meta_for_route(route, config);
    return [
      organization,
      website,
      buildArticleSchema(config, route, meta.title, meta.description, {
        about: ['Model Context Protocol', 'MCP', 'AI agents'],
      }),
    ];
  }

  if (route === '/ai-act-readiness') {
    const meta = get_meta_for_route(route, config);
    return [
      organization,
      website,
      buildArticleSchema(config, route, meta.title, meta.description, {
        about: ['EU AI Act', 'AI governance', 'AI agent approvals'],
      }),
    ];
  }

  if (route === '/resources/ai-agent-control-plane-2026') {
    const meta = get_meta_for_route(route, config);
    return [
      organization,
      website,
      buildArticleSchema(config, route, meta.title, meta.description, {
        about: [
          'AI agent control plane',
          'MCP firewall',
          'AI model gateway',
          'AI agent governance',
          'AgentOps',
          'EU AI Act readiness',
        ],
      }),
    ];
  }

  if (route.startsWith('/vs/')) {
    return [organization, website, build_vs_article_schema(config, route)];
  }

  if (route === '/privacy' || route === '/terms') {
    return [organization, website, buildWebPageSchema(route, config)];
  }

  return [organization, website, buildWebPageSchema(route, config)];
}
