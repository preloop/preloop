import { expect } from '@open-wc/testing';

import type { BrandConfig } from './brand-config';
import {
  buildAboutPageSchema,
  buildArticleSchema,
  buildFAQPageSchema,
  buildOrganizationSchema,
  buildProductSchema,
  buildSoftwareApplicationSchema,
  buildWebSiteSchema,
  get_meta_for_route,
  get_regulation_nav_links,
  get_regulation_slugs,
  get_route_from_filename,
  get_static_routes_with_options,
  get_structured_data_for_route,
} from './brand-seo';

const test_config: BrandConfig = {
  name: 'Preloop',
  domain: 'preloop.ai',
  edition: 'saas',
  company: {
    legal_name: 'Spacecode.AI, Inc.',
    address: '28 Geary St STE 650 Suite #235',
    city: 'San Francisco, CA 94108',
  },
  branding: {
    logo_light: '/images/logos/preloop_logo_light.svg',
    logo_dark: '/images/logos/preloop_logo_dark.svg',
    favicon: '/assets/preloop-badge.png',
    primary_color: '#7c3aed',
    gradient_product:
      'linear-gradient(90deg, hsl(270, 70%, 55%), hsl(290, 75%, 50%))',
    gradient_ai:
      'linear-gradient(90deg, hsl(200, 80%, 55%), hsl(180, 85%, 50%))',
  },
  social: {
    twitter: '@preloopai',
    linkedin: 'https://www.linkedin.com/company/preloop-ai/',
    instagram: 'https://www.instagram.com/preloop.ai',
  },
  landing: {
    meta: {
      title:
        'Preloop: AI agent governance, approvals, and AI Act readiness support',
      description:
        'Preloop helps engineering, platform, security, and operations teams govern AI agents in real workflows.',
      extended_description:
        'Preloop is an AI safety and control platform for teams deploying AI agents into real workflows.',
      keywords: 'Preloop, AI agent governance, AI Act readiness',
      og_image: '/assets/mcp-firewall.svg',
      og_title:
        'Preloop: Govern AI agents without slowing engineering teams down',
      og_description:
        'Onboard existing agents, govern tool access, and keep runtime activity visible.',
    },
    features_layout: 'grid',
    hero: {
      title: 'Onboard and Control AI Agents',
      lead: 'Govern AI agents in real workflows.',
      cta_primary: 'Start Free Trial',
      cta_secondary: 'Plan Your Rollout',
      cta_secondary_url: 'https://calendar.app.google/FV95tXZtfGpPk7398',
    },
    features: [
      {
        title: 'Govern tool access with policies and approvals',
        text: 'Define allow, deny, justification, and approval rules.',
        videoUrl: '',
        placeholderImg: '',
      },
    ],
    faqs: [
      {
        q: 'Can Preloop help with EU AI Act readiness?',
        a: 'Yes. See <a href="/ai-act-readiness">AI Act readiness with Preloop</a>.',
      },
      {
        q: 'Is Preloop open source?',
        a: 'Yes, Apache 2.0 licensed.',
      },
    ],
    get_started: {
      title: 'Onboard or connect your AI agent in minutes',
      link_text: 'See AI Act readiness guidance',
      link_url: '/ai-act-readiness',
      features: [],
      mcp_setup_title: 'Start with preloop agents discover',
      mcp_configs: [],
    },
  },
};

describe('brand-seo', () => {
  it('maps ai-act-readiness html files to the correct route', () => {
    expect(get_route_from_filename('/tmp/dist/ai-act-readiness.html')).to.equal(
      '/ai-act-readiness'
    );
  });

  it('returns route-specific AI Act metadata', () => {
    const meta = get_meta_for_route('/ai-act-readiness', test_config);

    expect(meta.title).to.equal('EU AI Act readiness with Preloop');
    expect(meta.description).to.include('AI governance programs');
    expect(meta.keywords).to.include('EU AI Act');
  });

  it('can exclude AI Act readiness from static routes when content is absent', () => {
    const routes = get_static_routes_with_options(test_config, []);

    expect(routes).to.include('/pricing');
    expect(routes).to.include('/about');
    expect(routes).to.not.include('/ai-act-readiness');
  });

  it('includes article structured data for the AI Act readiness page', () => {
    const structured_data = get_structured_data_for_route(
      '/ai-act-readiness',
      test_config
    );

    const article = structured_data.find(
      (entry) => entry['@type'] === 'Article'
    );

    expect(article).to.exist;
    expect(article?.headline).to.equal('EU AI Act readiness with Preloop');
    expect(article?.about).to.deep.equal([
      'EU AI Act',
      'Regulation (EU) 2024/1689',
      'AI agent approvals',
    ]);
  });

  it('maps CRA, DORA, and NIS2 html files to routes', () => {
    expect(get_route_from_filename('/tmp/dist/cra-readiness.html')).to.equal(
      '/cra-readiness'
    );
    expect(get_route_from_filename('/tmp/dist/dora.html')).to.equal('/dora');
    expect(get_route_from_filename('/tmp/dist/nis2.html')).to.equal('/nis2');
  });

  it('includes named-instrument routes when those slugs are supplied', () => {
    const routes = get_static_routes_with_options(test_config, [
      'ai-act-readiness',
      'cra-readiness',
      'dora',
      'nis2',
    ]);
    expect(routes).to.include('/ai-act-readiness');
    expect(routes).to.include('/cra-readiness');
    expect(routes).to.include('/dora');
    expect(routes).to.include('/nis2');
  });

  it('returns CRA metadata that names the instrument and date', () => {
    const meta = get_meta_for_route('/cra-readiness', test_config);
    expect(meta.title).to.include('Cyber Resilience Act');
    expect(meta.description).to.include('2024/2847');
    expect(meta.description).to.include('11 Sep 2026');
    expect(meta.keywords).to.include('CRA Art. 14');
  });

  it('names the brand, not Preloop, on white-label regulation pages', () => {
    const white_label: BrandConfig = {
      ...test_config,
      name: 'Acme Control',
      domain: 'acme.example',
    };

    for (const slug of get_regulation_slugs()) {
      const meta = get_meta_for_route(`/${slug}`, white_label);
      expect(meta.title, `${slug} title`).to.not.include('Preloop');
      expect(meta.og_title, `${slug} og:title`).to.not.include('Preloop');
      expect(meta.title, `${slug} title`).to.include('Acme Control');
    }

    const ai_act = get_meta_for_route('/ai-act-readiness', white_label);
    expect(ai_act.title).to.equal('EU AI Act readiness with Acme Control');
    expect(ai_act.description).to.include('Acme Control helps teams');
    expect(ai_act.description).to.not.include('Preloop');
    expect(ai_act.og_description).to.not.include('Preloop');

    const structured_data = get_structured_data_for_route(
      '/ai-act-readiness',
      white_label
    );
    const article = structured_data.find(
      (entry: any) => entry['@type'] === 'Article'
    ) as any;
    expect(article?.headline).to.equal('EU AI Act readiness with Acme Control');
  });

  it('builds footer links only for regulation pages that shipped', () => {
    expect(get_regulation_nav_links([])).to.deep.equal([]);
    expect(
      get_regulation_nav_links(['dora', 'not-a-regulation'])
    ).to.deep.equal([{ href: '/dora', label: 'DORA' }]);
    expect(
      get_regulation_nav_links(['ai-act-readiness', 'nis2'])
    ).to.deep.equal([
      { href: '/ai-act-readiness', label: 'EU AI Act' },
      { href: '/nis2', label: 'NIS2' },
    ]);
  });

  it('includes FAQ structured data on the homepage', () => {
    const structured_data = get_structured_data_for_route('/', test_config);

    const faq_page = structured_data.find(
      (entry) => entry['@type'] === 'FAQPage'
    );

    expect(faq_page).to.exist;
    expect(faq_page?.mainEntity).to.have.length(2);
  });
});

describe('brand-seo builders', () => {
  it('buildOrganizationSchema emits PostalAddress, sameAs, and logo from brand config', () => {
    const org = buildOrganizationSchema(test_config);

    expect(org['@type']).to.equal('Organization');
    expect(org['@id']).to.equal('https://preloop.ai/#organization');
    expect(org.name).to.equal('Preloop');
    expect(org.legalName).to.equal('Spacecode.AI, Inc.');
    expect(org.url).to.equal('https://preloop.ai/');
    expect(org.logo).to.equal(
      'https://preloop.ai/images/logos/preloop_logo_dark.svg'
    );
    expect(org.foundingDate).to.equal('2025');

    const address = org.address as Record<string, string>;
    expect(address['@type']).to.equal('PostalAddress');
    expect(address.streetAddress).to.equal('28 Geary St STE 650 Suite #235');
    expect(address.addressLocality).to.equal('San Francisco');
    expect(address.addressRegion).to.equal('CA');
    expect(address.postalCode).to.equal('94108');
    expect(address.addressCountry).to.equal('US');

    const same_as = org.sameAs as string[];
    expect(same_as).to.include('https://github.com/preloop/preloop');
    expect(same_as).to.include('https://twitter.com/preloopai');
    expect(same_as).to.include('https://www.linkedin.com/company/preloop-ai/');
    expect(same_as).to.include('https://www.instagram.com/preloop.ai');
  });

  it('buildWebSiteSchema references the Organization via @id', () => {
    const website = buildWebSiteSchema(test_config);

    expect(website['@type']).to.equal('WebSite');
    expect(website.url).to.equal('https://preloop.ai/');
    expect(website.name).to.equal('Preloop');
    const publisher = website.publisher as Record<string, string>;
    expect(publisher['@id']).to.equal('https://preloop.ai/#organization');
  });

  it('buildSoftwareApplicationSchema includes offers, featureList, and license', () => {
    const app = buildSoftwareApplicationSchema(test_config);

    expect(app['@type']).to.equal('SoftwareApplication');
    expect(app.applicationCategory).to.equal('DeveloperApplication');
    expect(app.applicationSubCategory).to.equal('AI Agent Governance');
    expect(app.license).to.equal('https://www.apache.org/licenses/LICENSE-2.0');
    expect(app.isAccessibleForFree).to.equal(true);
    expect(app.downloadUrl).to.equal('https://github.com/preloop/preloop');

    const alternate_names = app.alternateName as string[];
    expect(alternate_names).to.include('Preloop MCP Firewall');

    const offers = app.offers as Array<Record<string, string>>;
    expect(offers).to.have.length(3);
    expect(offers.map((o) => o.name)).to.deep.equal([
      'Open Source',
      'Teams',
      'Enterprise',
    ]);
    expect(offers[0].price).to.equal('0');
    expect(offers[1].price).to.equal('29');
    expect(offers[2].category).to.equal('Custom');

    const features = app.featureList as string[];
    expect(features.length).to.be.greaterThan(5);
    expect(features.some((f) => f.includes('MCP firewall'))).to.equal(true);
    expect(features.some((f) => f.includes('Apache 2.0'))).to.equal(true);

    // Aggregate rating must not appear unless real review data is wired up.
    expect(app.aggregateRating).to.equal(undefined);
  });

  it('buildFAQPageSchema strips HTML from faq answers and mirrors config.landing.faqs', () => {
    const faq_page = buildFAQPageSchema(test_config);

    expect(faq_page).to.exist;
    const main_entity = faq_page?.mainEntity as Array<Record<string, unknown>>;
    expect(main_entity).to.have.length(2);
    expect(main_entity[0].name).to.equal(test_config.landing.faqs[0].q);
    const accepted = main_entity[0].acceptedAnswer as Record<string, string>;
    expect(accepted.text).to.equal('Yes. See AI Act readiness with Preloop.');
    expect(accepted.text).to.not.match(/</);
  });

  it('buildFAQPageSchema returns undefined when there are no faqs', () => {
    const empty = buildFAQPageSchema({
      ...test_config,
      landing: { ...test_config.landing, faqs: [] },
    });
    expect(empty).to.equal(undefined);
  });

  it('buildAboutPageSchema references the organization via mainEntity @id', () => {
    const about = buildAboutPageSchema(test_config);
    expect(about['@type']).to.equal('AboutPage');
    expect(about.url).to.equal('https://preloop.ai/about');
    const main_entity = about.mainEntity as Record<string, string>;
    expect(main_entity['@id']).to.equal('https://preloop.ai/#organization');
  });

  it('buildArticleSchema emits required Article fields and defaults datePublished', () => {
    const article = buildArticleSchema(
      test_config,
      '/ai-act-readiness',
      'Demo headline',
      'Demo description',
      { about: ['EU AI Act'] }
    );

    expect(article['@type']).to.equal('Article');
    expect(article.headline).to.equal('Demo headline');
    expect(article.description).to.equal('Demo description');
    expect(article.url).to.equal('https://preloop.ai/ai-act-readiness');
    expect(article.about).to.deep.equal(['EU AI Act']);
    expect(article.datePublished).to.match(/^\d{4}-\d{2}-\d{2}$/);
    expect(article.isAccessibleForFree).to.equal(true);
  });

  it('buildProductSchema falls back to default offers when pricing.plans absent', () => {
    const product = buildProductSchema(test_config);
    expect(product['@type']).to.equal('Product');
    expect(product.url).to.equal('https://preloop.ai/pricing');
    const offers = product.offers as Array<Record<string, string>>;
    expect(offers).to.have.length(3);
    expect(offers[0].name).to.equal('Open Source');
  });

  it('buildProductSchema uses config.landing.pricing.plans when available', () => {
    const config_with_plans: BrandConfig = {
      ...test_config,
      landing: {
        ...test_config.landing,
        pricing: {
          enabled: true,
          plans: [
            {
              id: 'free',
              name: 'Community',
              price_monthly: 0,
              price_annually: 0,
              features: [],
            },
            {
              id: 'teams',
              name: 'Teams',
              price_monthly: 49,
              price_annually: 490,
              description: 'Per user, monthly',
              features: [],
            },
          ],
        },
      },
    };

    const product = buildProductSchema(config_with_plans);
    const offers = product.offers as Array<Record<string, string>>;
    expect(offers).to.have.length(2);
    expect(offers[0].name).to.equal('Community');
    expect(offers[0].category).to.equal('Free');
    expect(offers[1].price).to.equal('49');
    expect(offers[1].description).to.include('Per user');
  });
});

describe('get_structured_data_for_route', () => {
  it('returns an array of at least 3 entries for the landing route including SoftwareApplication and FAQPage', () => {
    const entries = get_structured_data_for_route('/', test_config);

    expect(Array.isArray(entries)).to.equal(true);
    expect(entries.length).to.be.greaterThan(2);

    const types = entries.map((entry) => entry['@type']);
    expect(types).to.include('Organization');
    expect(types).to.include('WebSite');
    expect(types).to.include('SoftwareApplication');
    expect(types).to.include('FAQPage');
  });

  it('uses Product schema for the pricing route', () => {
    const entries = get_structured_data_for_route('/pricing', test_config);
    const types = entries.map((entry) => entry['@type']);
    expect(types).to.include('Organization');
    expect(types).to.include('WebSite');
    expect(types).to.include('Product');
  });

  it('uses AboutPage schema for the about route', () => {
    const entries = get_structured_data_for_route('/about', test_config);
    const types = entries.map((entry) => entry['@type']);
    expect(types).to.include('AboutPage');
  });

  it('emits Organization + WebSite + WebPage for privacy and terms', () => {
    for (const route of ['/privacy', '/terms']) {
      const entries = get_structured_data_for_route(route, test_config);
      const types = entries.map((entry) => entry['@type']);
      expect(types).to.deep.equal(['Organization', 'WebSite', 'WebPage']);
    }
  });

  it('emits an Article schema with mentions for /vs/<slug> routes', () => {
    const entries = get_structured_data_for_route(
      '/vs/aws-bedrock-agentcore',
      test_config
    );
    const article = entries.find((entry) => entry['@type'] === 'Article');
    expect(article).to.exist;
    expect(article?.headline).to.equal('Preloop vs Aws Bedrock Agentcore');
    const mentions = article?.mentions as Array<Record<string, string>>;
    expect(mentions).to.have.length(2);
    expect(mentions[0].name).to.equal('Aws Bedrock Agentcore');
  });
});
