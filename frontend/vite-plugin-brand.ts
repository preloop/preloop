import { Plugin, IndexHtmlTransformContext } from 'vite';
import * as yaml from 'js-yaml';
import * as fs from 'fs';
import * as path from 'path';
import { BrandConfig } from './src/brand-config';
import {
  get_canonical_url,
  get_meta_for_route,
  get_route_from_filename,
  get_static_routes_with_options,
  get_structured_data_for_route,
  VS_PAGE_META,
} from './src/brand-seo';

/**
 * Enumerate competitor comparison slugs that have both a markdown file on
 * disk AND a `VS_PAGE_META` registration. Kept as a plugin-scope helper so
 * `generateBundle`, `closeBundle`, sitemap, and llms.txt all see the exact
 * same set of slugs.
 */
function discover_vs_slugs(
  contentBasePath: string,
  brandKey: string
): string[] {
  const vsDir = path.resolve(contentBasePath, brandKey, 'vs');
  if (!fs.existsSync(vsDir)) {
    return [];
  }
  return fs
    .readdirSync(vsDir)
    .filter((name) => name.endsWith('.md'))
    .map((name) => name.replace(/\.md$/, ''))
    .filter((slug) => Boolean(VS_PAGE_META[slug]))
    .sort();
}

/**
 * Options for the brand plugin
 */
export interface BrandPluginOptions {
  /** Path to brands.yaml file (default: ./brands.yaml relative to this plugin) */
  configPath?: string;
  /** Path to content directory (default: ./content relative to this plugin) */
  contentPath?: string;
  /** Output directory name (default: 'dist') */
  outDir?: string;
}

/**
 * Vite plugin to inject brand-specific content and configuration
 *
 * This plugin:
 * 1. Loads brand configuration from brands.yaml
 * 2. Injects brand config as window.BRAND_CONFIG
 * 3. Transforms index.html with route-specific slotted content for SEO
 * 4. Updates meta tags with brand-specific values per route
 *
 * @param brandKey - The brand key to use from brands.yaml (e.g., 'preloop')
 * @param options - Optional configuration for custom paths
 */
export function brandPlugin(
  brandKey: string,
  options: BrandPluginOptions = {}
): Plugin {
  let brandConfig: BrandConfig;

  // Resolve paths - use options or defaults
  const configPath =
    options.configPath || path.resolve(__dirname, 'brands.yaml');
  const contentBasePath =
    options.contentPath || path.resolve(__dirname, 'content');
  const outDirPath = options.outDir || path.resolve(__dirname, 'dist');

  return {
    name: 'vite-plugin-brand',

    configResolved() {
      // Load brand configuration at build time
      if (!fs.existsSync(configPath)) {
        throw new Error(`brands.yaml not found at ${configPath}`);
      }

      const brandsYaml = fs.readFileSync(configPath, 'utf-8');
      const brands = yaml.load(brandsYaml) as any;

      if (!brands || !brands.brands) {
        throw new Error(
          'Invalid brands.yaml structure: brands.brands not found'
        );
      }

      brandConfig = brands.brands[brandKey];

      if (!brandConfig) {
        throw new Error(
          `Brand "${brandKey}" not found in brands.yaml. Available brands: ${Object.keys(brands.brands).join(', ')}`
        );
      }

      // Apply defaults for missing optional fields
      brandConfig.company = brandConfig.company || {};
      brandConfig.social = brandConfig.social || {};
      brandConfig.landing = brandConfig.landing || ({} as any);
      brandConfig.landing.meta = brandConfig.landing.meta || ({} as any);
      brandConfig.landing.hero = brandConfig.landing.hero || ({} as any);
      brandConfig.landing.features = brandConfig.landing.features || [];
      brandConfig.landing.faqs = brandConfig.landing.faqs || [];
      brandConfig.landing.get_started =
        brandConfig.landing.get_started || ({} as any);
      brandConfig.landing.get_started.features =
        brandConfig.landing.get_started.features || [];
      brandConfig.landing.get_started.cli_setup =
        brandConfig.landing.get_started.cli_setup || [];
      brandConfig.landing.pricing = brandConfig.landing.pricing || ({} as any);
      brandConfig.landing.pricing!.plans =
        brandConfig.landing.pricing!.plans || [];
      brandConfig.landing.pricing!.faqs =
        brandConfig.landing.pricing!.faqs || [];

      console.log(
        `\n🎨 Building for brand: ${brandConfig.name} (${brandConfig.domain})\n`
      );
    },

    async generateBundle(options, bundle) {
      // Generate landing content JSON file with safe defaults
      const landingContent = {
        hero: brandConfig.landing.hero || {},
        extended_description:
          brandConfig.landing.meta?.extended_description || '',
        features_layout: brandConfig.landing.features_layout || 'grid',
        features: brandConfig.landing.features || [],
        faqs: brandConfig.landing.faqs || [],
        get_started: brandConfig.landing.get_started || {},
        product_hunt: (brandConfig.landing as any).product_hunt || null,
        featured_video: (brandConfig.landing as any).featured_video || null,
        pricing: brandConfig.landing.pricing || null,
      };
      const aiActReadinessMdPath = path.resolve(
        contentBasePath,
        `${brandKey}/ai-act-readiness.md`
      );
      const hasAiActReadinessPage = fs.existsSync(aiActReadinessMdPath);
      // Competitor comparison pages (/vs/<slug>) are SaaS-only.
      const vsSlugsForRouting =
        (brandConfig as any).edition === 'saas' || !(brandConfig as any).edition
          ? discover_vs_slugs(contentBasePath, brandKey)
          : [];

      // Add JSON file to bundle
      this.emitFile({
        type: 'asset',
        fileName: 'landing-content.json',
        source: JSON.stringify(landingContent, null, 2),
      });

      this.emitFile({
        type: 'asset',
        fileName: 'sitemap.xml',
        source: generateSitemapXml(
          brandConfig,
          hasAiActReadinessPage,
          vsSlugsForRouting
        ),
      });

      this.emitFile({
        type: 'asset',
        fileName: 'robots.txt',
        source: generateRobotsTxt(brandConfig),
      });

      this.emitFile({
        type: 'asset',
        fileName: 'llms.txt',
        source: generateLlmsTxt(
          brandConfig,
          hasAiActReadinessPage,
          vsSlugsForRouting
        ),
      });

      // Generate static HTML fragments for dynamic loading
      const privacyHTML = await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'privacy'
      );

      this.emitFile({
        type: 'asset',
        fileName: 'content/privacy.html',
        source: privacyHTML,
      });

      // Only generate pricing content for SaaS editions with pricing enabled
      const edition = (brandConfig as any).edition || 'saas';
      if (
        edition === 'saas' &&
        brandConfig.landing.pricing?.enabled !== false
      ) {
        const pricingHTML = generatePricingSlottedContent(brandConfig);
        this.emitFile({
          type: 'asset',
          fileName: 'content/pricing.html',
          source: pricingHTML,
        });
      }

      // Copy brand-specific markdown files to dist/content/ for dynamic loading
      // Use the brand key (e.g., 'preloop') to find content folder
      const contentFiles = [
        'privacy.md',
        'terms.md',
        'whatis-mcp.md',
        'ai-act-readiness.md',
      ];

      for (const file of contentFiles) {
        const contentFilePath = path.resolve(
          contentBasePath,
          `${brandKey}/${file}`
        );
        if (fs.existsSync(contentFilePath)) {
          const markdown = fs.readFileSync(contentFilePath, 'utf-8');
          this.emitFile({
            type: 'asset',
            fileName: `content/${file}`,
            source: markdown,
          });
        }
      }

      // Mirror competitor comparison markdown files into dist/content/vs/ so
      // the SPA router can fetch them for client-side navigation to /vs/<slug>.
      for (const slug of vsSlugsForRouting) {
        const vsMdPath = path.resolve(
          contentBasePath,
          `${brandKey}/vs/${slug}.md`
        );
        if (fs.existsSync(vsMdPath)) {
          const markdown = fs.readFileSync(vsMdPath, 'utf-8');
          this.emitFile({
            type: 'asset',
            fileName: `content/vs/${slug}.md`,
            source: markdown,
          });
        }
      }
    },

    async closeBundle() {
      // After all files are written, generate full HTML pages for static content
      // Read the generated index.html as a template
      // Use the configured output directory
      const indexHtmlPath = path.resolve(outDirPath, 'index.html');

      if (!fs.existsSync(indexHtmlPath)) {
        console.warn(
          `index.html not found at ${indexHtmlPath}, cannot generate standalone HTML pages`
        );
        return;
      }

      const indexHtml = fs.readFileSync(indexHtmlPath, 'utf-8');

      // Generate static markdown content HTML
      // Use brandKey for content folder lookup
      const privacyHTML = await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'privacy'
      );
      const termsHTML = await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'terms'
      );
      const whatisMcpHTML = await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'whatis-mcp'
      );
      const edition = (brandConfig as any).edition || 'saas';
      const aiActReadinessMdPath = path.resolve(
        contentBasePath,
        `${brandKey}/ai-act-readiness.md`
      );
      const hasAiActReadinessPage = fs.existsSync(aiActReadinessMdPath);
      const aiActReadinessHTML = hasAiActReadinessPage
        ? await loadMarkdownContent(
            contentBasePath,
            brandKey,
            'ai-act-readiness'
          )
        : '';

      // Generate privacy.html with proper meta tags and content
      const privacyPage = generateFullHtmlPage(
        indexHtml,
        '/privacy',
        brandConfig,
        privacyHTML
      );
      fs.writeFileSync(path.resolve(outDirPath, 'privacy.html'), privacyPage);

      // Generate terms.html
      const termsPage = generateFullHtmlPage(
        indexHtml,
        '/terms',
        brandConfig,
        termsHTML
      );
      fs.writeFileSync(path.resolve(outDirPath, 'terms.html'), termsPage);

      // Generate whatis-mcp.html
      const whatisMcpPage = generateFullHtmlPage(
        indexHtml,
        '/whatis-mcp',
        brandConfig,
        whatisMcpHTML
      );
      fs.writeFileSync(
        path.resolve(outDirPath, 'whatis-mcp.html'),
        whatisMcpPage
      );

      if (edition === 'saas' && hasAiActReadinessPage) {
        // Generate ai-act-readiness.html
        const aiActReadinessPage = generateFullHtmlPage(
          indexHtml,
          '/ai-act-readiness',
          brandConfig,
          aiActReadinessHTML
        );
        fs.writeFileSync(
          path.resolve(outDirPath, 'ai-act-readiness.html'),
          aiActReadinessPage
        );
      }

      // Generate additional pages for SaaS editions
      const generatedPages = ['privacy.html', 'terms.html', 'whatis-mcp.html'];
      if (edition === 'saas' && hasAiActReadinessPage) {
        generatedPages.push('ai-act-readiness.html');
      }

      if (
        edition === 'saas' &&
        brandConfig.landing.pricing?.enabled !== false
      ) {
        // Generate pricing.html with slotted SEO content that <public-pricing-view>
        // projects into the interactive UI on hydration.
        const pricingHTML = generatePricingSlottedContent(brandConfig);
        const pricingPage = generateFullHtmlPage(
          indexHtml,
          '/pricing',
          brandConfig,
          pricingHTML
        );
        fs.writeFileSync(path.resolve(outDirPath, 'pricing.html'), pricingPage);
        generatedPages.push('pricing.html');

        // Generate about.html
        const aboutHTML = await loadMarkdownContent(
          contentBasePath,
          brandKey,
          'about'
        );
        if (aboutHTML) {
          const aboutPage = generateFullHtmlPage(
            indexHtml,
            '/about',
            brandConfig,
            aboutHTML
          );
          fs.writeFileSync(path.resolve(outDirPath, 'about.html'), aboutPage);
          generatedPages.push('about.html');

          // Also copy about.md to content folder for client-side navigation
          const aboutMdPath = path.resolve(
            contentBasePath,
            brandKey,
            'about.md'
          );
          if (fs.existsSync(aboutMdPath)) {
            const contentDir = path.resolve(outDirPath, 'content');
            fs.copyFileSync(aboutMdPath, path.resolve(contentDir, 'about.md'));
          }
        }

        if (hasAiActReadinessPage) {
          const contentDir = path.resolve(outDirPath, 'content');
          fs.copyFileSync(
            aiActReadinessMdPath,
            path.resolve(contentDir, 'ai-act-readiness.md')
          );
        }

        // Generate long-form resource pages (pillar articles). These live at
        // /resources/<slug> and are sourced from content/<brand>/resources/.
        const resourcePages = [
          {
            slug: 'ai-agent-control-plane-2026',
            route: '/resources/ai-agent-control-plane-2026',
          },
        ];

        for (const resource of resourcePages) {
          const resourceMdPath = path.resolve(
            contentBasePath,
            brandKey,
            `resources/${resource.slug}.md`
          );
          if (!fs.existsSync(resourceMdPath)) {
            continue;
          }

          const resourceHTML = await loadMarkdownContent(
            contentBasePath,
            brandKey,
            `resources/${resource.slug}`
          );
          const resourcePage = generateFullHtmlPage(
            indexHtml,
            resource.route,
            brandConfig,
            resourceHTML
          );

          const resourcesDir = path.resolve(outDirPath, 'resources');
          if (!fs.existsSync(resourcesDir)) {
            fs.mkdirSync(resourcesDir, { recursive: true });
          }
          fs.writeFileSync(
            path.resolve(resourcesDir, `${resource.slug}.html`),
            resourcePage
          );
          generatedPages.push(`resources/${resource.slug}.html`);

          // Copy markdown to content/resources for client-side navigation
          const contentResourcesDir = path.resolve(
            outDirPath,
            'content',
            'resources'
          );
          if (!fs.existsSync(contentResourcesDir)) {
            fs.mkdirSync(contentResourcesDir, { recursive: true });
          }
          fs.copyFileSync(
            resourceMdPath,
            path.resolve(contentResourcesDir, `${resource.slug}.md`)
          );
        }

        // Generate competitor comparison landing pages at /vs/<slug>. Sources
        // are markdown files under content/<brand>/vs/ that have a matching
        // VS_PAGE_META registration. Each slug becomes a standalone crawlable
        // dist/vs/<slug>.html plus a dist/content/vs/<slug>.md for SPA nav.
        const vsSlugs = discover_vs_slugs(contentBasePath, brandKey);
        if (vsSlugs.length > 0) {
          const vsOutDir = path.resolve(outDirPath, 'vs');
          const contentVsDir = path.resolve(outDirPath, 'content', 'vs');
          if (!fs.existsSync(vsOutDir)) {
            fs.mkdirSync(vsOutDir, { recursive: true });
          }
          if (!fs.existsSync(contentVsDir)) {
            fs.mkdirSync(contentVsDir, { recursive: true });
          }

          for (const slug of vsSlugs) {
            const vsMdPath = path.resolve(
              contentBasePath,
              brandKey,
              `vs/${slug}.md`
            );
            if (!fs.existsSync(vsMdPath)) {
              continue;
            }

            const vsHTML = await loadMarkdownContent(
              contentBasePath,
              brandKey,
              `vs/${slug}`
            );
            const vsPage = generateFullHtmlPage(
              indexHtml,
              `/vs/${slug}`,
              brandConfig,
              vsHTML
            );
            fs.writeFileSync(path.resolve(vsOutDir, `${slug}.html`), vsPage);
            generatedPages.push(`vs/${slug}.html`);

            fs.copyFileSync(vsMdPath, path.resolve(contentVsDir, `${slug}.md`));
          }
        }
      }

      console.log(
        `✓ Generated standalone HTML pages: ${generatedPages.join(', ')}`
      );
    },

    async transformIndexHtml(html, ctx) {
      // Determine which route we're rendering based on the filename
      const filename = ctx.filename || '';
      const route = get_route_from_filename(filename);

      // Get route-specific metadata
      const meta = get_meta_for_route(route, brandConfig);
      const canonicalUrl = get_canonical_url(route, brandConfig);

      // Replace <title>
      html = html.replace(
        /<title>.*?<\/title>/,
        `<title>${meta.title}</title>`
      );

      // Replace meta description. index.html wraps the `content` attribute
      // onto a second line (formatter-driven), so the regex must tolerate
      // whitespace — including newlines — between attributes and inside the
      // attribute value itself.
      html = html.replace(
        /<meta\s+name="description"\s+content="[\s\S]*?">/,
        `<meta name="description" content="${meta.description}">`
      );

      // Replace meta keywords
      html = html.replace(
        /<meta\s+name="keywords"\s+content="[\s\S]*?">/,
        `<meta name="keywords" content="${meta.keywords}">`
      );

      // Replace Open Graph title
      html = html.replace(
        /<meta\s+property="og:title"\s+content="[\s\S]*?">/,
        `<meta property="og:title" content="${meta.og_title}">`
      );

      // Replace Open Graph description
      html = html.replace(
        /<meta\s+property="og:description"\s+content="[\s\S]*?">/,
        `<meta property="og:description" content="${meta.og_description}">`
      );

      // Replace Open Graph image
      html = html.replace(
        /<meta\s+property="og:image"\s+content="[\s\S]*?">/,
        `<meta property="og:image" content="${meta.og_image}">`
      );

      // Replace Open Graph URL
      html = html.replace(
        /<meta\s+property="og:url"\s+content="[\s\S]*?">/,
        `<meta property="og:url" content="${canonicalUrl}">`
      );

      html = upsertHeadTag(
        html,
        /<link\s+rel="canonical"\s+href="[\s\S]*?">/,
        `<link rel="canonical" href="${canonicalUrl}">`
      );

      html = upsertHeadTag(
        html,
        /<meta\s+name="robots"\s+content="[\s\S]*?">/,
        '<meta name="robots" content="index, follow">'
      );

      // Replace Twitter card title
      html = html.replace(
        /<meta\s+name="twitter:title"\s+content="[\s\S]*?">/,
        `<meta name="twitter:title" content="${meta.title}">`
      );

      // Replace Twitter card description
      html = html.replace(
        /<meta\s+name="twitter:description"\s+content="[\s\S]*?">/,
        `<meta name="twitter:description" content="${meta.description}">`
      );

      // Replace Twitter card image
      html = html.replace(
        /<meta\s+name="twitter:image"\s+content="[\s\S]*?">/,
        `<meta name="twitter:image" content="${meta.og_image}">`
      );

      // Replace Twitter site handle
      html = html.replace(
        /<meta\s+name="twitter:site"\s+content="[\s\S]*?">/,
        `<meta name="twitter:site" content="${brandConfig.social.twitter}">`
      );

      // Replace Twitter creator handle
      html = html.replace(
        /<meta\s+name="twitter:creator"\s+content="[\s\S]*?">/,
        `<meta name="twitter:creator" content="${brandConfig.social.twitter}">`
      );

      html = upsertStructuredDataTag(html, route, brandConfig);

      // Replace favicon
      html = html.replace(
        /\/images\/favicon\.png/g,
        brandConfig.branding.favicon
      );

      // Inject minimal runtime brand configuration (no content duplication)
      // Only includes styling/branding metadata, not SEO content
      const runtimeConfig = {
        name: brandConfig.name,
        domain: brandConfig.domain,
        edition: (brandConfig as any).edition || 'saas', // Default to 'saas' for backwards compatibility
        branding: brandConfig.branding,
        social: brandConfig.social,
        company: brandConfig.company,
      };

      const brandScript = `
  <script>
    window.BRAND_CONFIG = ${JSON.stringify(runtimeConfig, null, 2)};
  </script>`;

      html = html.replace('</head>', `${brandScript}\n</head>`);

      // Inject route-specific content for SSR
      const slottedContent = await generateSlottedContentForRoute(
        route,
        brandConfig,
        brandKey,
        contentBasePath
      );
      if (slottedContent) {
        if (route === '/') {
          // Landing page: inject landing-view with slots
          html = html.replace(
            '<lit-app></lit-app>',
            `<lit-app data-ssr-route="/"><landing-view>${slottedContent}</landing-view></lit-app>`
          );
        } else if (route === '/pricing') {
          // Pricing page: interactive component with slotted SEO fallback
          html = html.replace(
            '<lit-app></lit-app>',
            `<lit-app data-ssr-route="${route}"><public-pricing-view>${slottedContent}</public-pricing-view></lit-app>`
          );
        } else if (
          route === '/privacy' ||
          route === '/ai-act-readiness' ||
          route === '/resources/ai-agent-control-plane-2026' ||
          route.startsWith('/vs/')
        ) {
          // Static pages: inject static-view-wrapper with content
          html = html.replace(
            '<lit-app></lit-app>',
            `<lit-app data-ssr-route="${route}"><static-view-wrapper>${slottedContent}</static-view-wrapper></lit-app>`
          );
        } else {
          // No SSR for other routes
          html = html.replace('<lit-app></lit-app>', `<lit-app></lit-app>`);
        }
      }

      return html;
    },
  };
}

/**
 * Generate route-specific slotted HTML content for SEO
 * Content uses named slots that web components can consume
 */
async function generateSlottedContentForRoute(
  route: string,
  config: BrandConfig,
  brandKey: string,
  contentBasePath: string
): Promise<string> {
  // Safe accessors with defaults
  const hero = config.landing?.hero || {};
  const meta = config.landing?.meta || {};
  const features = config.landing?.features || [];
  const faqs = config.landing?.faqs || [];
  const getStarted = config.landing?.get_started || {};
  const getStartedFeatures = getStarted.features || [];
  const cliSetup = getStarted.cli_setup || [];

  switch (route) {
    case '/':
      // Landing page - generate slotted content for landing-view component
      return `
    <!-- SEO Content - Slotted for web components to consume -->
    <!-- Landing-view component will read and display this content -->

    <!-- Hero content slots -->
    <h1 slot="hero-title">${hero.title || ''}</h1>
    <p slot="hero-lead">${hero.lead || ''}</p>
    <span slot="cta-primary">${hero.cta_primary || ''}</span>
    <span slot="cta-primary-url">${(hero as any).cta_primary_url || ''}</span>
    <span slot="cta-secondary">${hero.cta_secondary || ''}</span>
    <span slot="cta-secondary-url">${hero.cta_secondary_url || ''}</span>
    ${(hero as any).install_command ? `<code slot="cta-install">${(hero as any).install_command}</code>` : ''}
    ${(hero as any).install_caption ? `<span slot="cta-install-caption">${(hero as any).install_caption}</span>` : ''}

    <!-- Extended description slot (only if exists) -->
    ${meta.extended_description ? `<p slot="extended-description">${meta.extended_description}</p>` : ''}

    <!-- Features layout slot -->
    <span slot="features-layout">${config.landing?.features_layout || 'grid'}</span>

    <!-- Feature slots -->
    ${features
      .map(
        (feature, idx) => `
    <div slot="feature-${idx}" data-title="${feature.title || ''}" data-text="${feature.text || ''}" data-video="${feature.videoUrl || ''}" data-img="${feature.placeholderImg || ''}">
      <h3>${feature.title || ''}</h3>
      <p>${feature.text || ''}</p>
    </div>`
      )
      .join('\n')}

    <!-- FAQ slots -->
    ${faqs
      .map(
        (faq, idx) => `
    <div slot="faq-${idx}" data-q="${faq.q || ''}" data-a="${faq.a || ''}">
      <h3>${faq.q || ''}</h3>
      <p>${faq.a || ''}</p>
    </div>`
      )
      .join('\n')}

    <!-- Get Started section slots -->
    <span slot="get-started-title">${getStarted.title || ''}</span>
    <span slot="get-started-link-text">${getStarted.link_text || ''}</span>
    <span slot="get-started-link-url">${getStarted.link_url || ''}</span>

    <!-- Get Started feature slots -->
    ${getStartedFeatures
      .map(
        (feature, idx) => `
    <div slot="get-started-feature-${idx}" data-icon="${feature.icon || ''}" data-title="${feature.title || ''}" data-text="${feature.text || ''}">
      <h3>${feature.title || ''}</h3>
      <p>${feature.text || ''}</p>
    </div>`
      )
      .join('\n')}

    <!-- MCP Setup slots -->
    <span slot="mcp-setup-title">${getStarted.mcp_setup_title || ''}</span>

    <!-- CLI Setup slots -->
    ${cliSetup
      .map(
        (step, idx) => `
    <div slot="cli-setup-${idx}"
         data-step="${step.step || ''}"
         data-command="${step.command || ''}">
    </div>`
      )
      .join('\n')}

    <!-- Product Hunt slot -->
    ${(() => {
      const productHunt = (config.landing as any).product_hunt;
      if (productHunt?.enabled) {
        return `
    <div slot="product-hunt"
         data-enabled="true"
         data-post-id="${productHunt.post_id || ''}"
         data-theme="${productHunt.theme || 'light'}">
      <a href="https://www.producthunt.com/products/preloop?embed=true&amp;utm_source=badge-featured&amp;utm_medium=badge&amp;utm_campaign=badge-preloop" target="_blank" rel="noopener noreferrer">
        <img alt="Preloop - The MCP Governance Layer | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=${productHunt.post_id}&amp;theme=${productHunt.theme}" />
      </a>
    </div>`;
      }
      return '';
    })()}

    <!-- Featured Video slot -->
    ${(() => {
      const featuredVideo = (config.landing as any).featured_video;
      if (featuredVideo?.enabled) {
        return `
    <div slot="featured-video"
         data-enabled="true"
         data-title="${featuredVideo.title || ''}"
         data-youtube-url="${featuredVideo.youtube_url || ''}"
         data-youtube-embed="${featuredVideo.youtube_embed || ''}">
      ${featuredVideo.title ? `<h2>${featuredVideo.title}</h2>` : ''}
      <iframe width="560" height="315" src="${featuredVideo.youtube_embed}" title="YouTube video player" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>
    </div>`;
      }
      return '';
    })()}
  `;

    case '/privacy':
      // Privacy page - will load markdown content
      return await loadMarkdownContent(contentBasePath, brandKey, 'privacy');

    case '/pricing':
      // Pricing page - emit slotted light-DOM content that
      // <public-pricing-view> can project for SEO and no-JS users.
      return generatePricingSlottedContent(config);

    case '/ai-act-readiness':
      return await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'ai-act-readiness'
      );

    case '/resources/ai-agent-control-plane-2026':
      return await loadMarkdownContent(
        contentBasePath,
        brandKey,
        'resources/ai-agent-control-plane-2026'
      );

    default: {
      // Competitor comparison landing pages at /vs/<slug>. Load the matching
      // markdown fragment so the pre-rendered HTML ships with real content.
      if (route.startsWith('/vs/')) {
        const slug = route.slice('/vs/'.length);
        if (slug && !slug.includes('/') && VS_PAGE_META[slug]) {
          const vsMdPath = path.resolve(
            contentBasePath,
            brandKey,
            `vs/${slug}.md`
          );
          if (fs.existsSync(vsMdPath)) {
            return await loadMarkdownContent(
              contentBasePath,
              brandKey,
              `vs/${slug}`
            );
          }
        }
      }
      return '';
    }
  }
}

/**
 * Generate a full standalone HTML page for a route
 * Takes the base index.html and replaces meta tags and content for the route
 */
function generateFullHtmlPage(
  indexHtml: string,
  route: string,
  config: BrandConfig,
  content: string
): string {
  const meta = get_meta_for_route(route, config);
  const canonicalUrl = get_canonical_url(route, config);
  let html = indexHtml;

  // Replace <title>
  html = html.replace(/<title>.*?<\/title>/, `<title>${meta.title}</title>`);

  // Replace meta description. The regex tolerates newlines and arbitrary
  // whitespace between attributes because index.html sometimes wraps the
  // `content` attribute onto its own line.
  html = html.replace(
    /<meta\s+name="description"\s+content="[\s\S]*?">/,
    `<meta name="description" content="${meta.description}">`
  );

  // Replace meta keywords
  html = html.replace(
    /<meta\s+name="keywords"\s+content="[\s\S]*?">/,
    `<meta name="keywords" content="${meta.keywords}">`
  );

  // Replace Open Graph title
  html = html.replace(
    /<meta\s+property="og:title"\s+content="[\s\S]*?">/,
    `<meta property="og:title" content="${meta.og_title}">`
  );

  // Replace Open Graph description
  html = html.replace(
    /<meta\s+property="og:description"\s+content="[\s\S]*?">/,
    `<meta property="og:description" content="${meta.og_description}">`
  );

  // Replace Open Graph image
  html = html.replace(
    /<meta\s+property="og:image"\s+content="[\s\S]*?">/,
    `<meta property="og:image" content="${meta.og_image}">`
  );

  // Replace Open Graph URL
  html = html.replace(
    /<meta\s+property="og:url"\s+content="[\s\S]*?">/,
    `<meta property="og:url" content="${canonicalUrl}">`
  );

  html = upsertHeadTag(
    html,
    /<link\s+rel="canonical"\s+href="[\s\S]*?">/,
    `<link rel="canonical" href="${canonicalUrl}">`
  );

  html = upsertHeadTag(
    html,
    /<meta\s+name="robots"\s+content="[\s\S]*?">/,
    '<meta name="robots" content="index, follow">'
  );

  // Replace Twitter card title
  html = html.replace(
    /<meta\s+name="twitter:title"\s+content="[\s\S]*?">/,
    `<meta name="twitter:title" content="${meta.title}">`
  );

  // Replace Twitter card description
  html = html.replace(
    /<meta\s+name="twitter:description"\s+content="[\s\S]*?">/,
    `<meta name="twitter:description" content="${meta.description}">`
  );

  // Replace Twitter card image
  html = html.replace(
    /<meta\s+name="twitter:image"\s+content="[\s\S]*?">/,
    `<meta name="twitter:image" content="${meta.og_image}">`
  );

  html = upsertStructuredDataTag(html, route, config);

  // Replace <lit-app> with content-wrapped version. The pricing route is
  // special-cased: instead of the read-only static-view-wrapper, we wrap the
  // slotted SEO content in <public-pricing-view> so the interactive pricing
  // component can hydrate on top of it without losing the crawlable fallback.
  const wrapperTag =
    route === '/pricing' ? 'public-pricing-view' : 'static-view-wrapper';
  html = html.replace(
    /<lit-app[^>]*>[\s\S]*?<\/lit-app>/,
    `<lit-app data-ssr-route="${route}"><${wrapperTag}>${content}</${wrapperTag}></lit-app>`
  );

  return html;
}

function upsertHeadTag(html: string, pattern: RegExp, tag: string): string {
  if (pattern.test(html)) {
    return html.replace(pattern, tag);
  }

  return html.replace('</head>', `  ${tag}\n</head>`);
}

function upsertStructuredDataTag(
  html: string,
  route: string,
  config: BrandConfig
): string {
  const structuredData = JSON.stringify(
    get_structured_data_for_route(route, config)
  )
    .replaceAll('<', '\\u003c')
    .replaceAll('</script', '<\\/script');

  return upsertHeadTag(
    html,
    /<script id="preloop-structured-data" type="application\/ld\+json">[\s\S]*?<\/script>/,
    `<script id="preloop-structured-data" type="application/ld+json">${structuredData}</script>`
  );
}

function generateSitemapXml(
  config: BrandConfig,
  includeAiActReadiness: boolean,
  vsSlugs: string[] = []
): string {
  const routes = get_static_routes_with_options(
    config,
    includeAiActReadiness,
    vsSlugs
  );
  const urls = routes
    .map(
      (route) =>
        `  <url>\n    <loc>https://${config.domain}${route}</loc>\n  </url>`
    )
    .join('\n');

  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`;
}

function generateRobotsTxt(config: BrandConfig): string {
  return `User-agent: *\nAllow: /\n\nSitemap: https://${config.domain}/sitemap.xml\n`;
}

function generateLlmsTxt(
  config: BrandConfig,
  includeAiActReadiness: boolean,
  vsSlugs: string[] = []
): string {
  const meta = config.landing?.meta || {};
  const hero = config.landing?.hero || {};
  const routes = get_static_routes_with_options(
    config,
    includeAiActReadiness,
    vsSlugs
  );

  return [
    `# ${config.name}`,
    '',
    meta.description || '',
    '',
    'Primary pages:',
    ...routes.map((route) => `- https://${config.domain}${route}`),
    '',
    'Primary calls to action:',
    `- ${hero.cta_primary || 'Sign up'} -> https://${config.domain}/register`,
    `- ${hero.cta_secondary || 'Request demo'} -> ${(hero as any).cta_secondary_url || `https://${config.domain}/request-demo`}`,
    '',
  ].join('\n');
}

/**
 * Load and convert markdown file to HTML using marked
 */
async function loadMarkdownContent(
  contentBasePath: string,
  brandName: string,
  filename: string
): Promise<string> {
  const contentPath = path.resolve(
    contentBasePath,
    `${brandName}/${filename}.md`
  );

  if (!fs.existsSync(contentPath)) {
    console.warn(`Warning: Markdown file not found at ${contentPath}`);
    return `<article class="container py-5"><h1>Content Not Found</h1><p>The requested content could not be loaded.</p></article>`;
  }

  const markdown = fs.readFileSync(contentPath, 'utf-8');

  // Dynamically import marked (ESM module)
  const { marked } = await import('marked');
  const html = await marked.parse(markdown);

  // These styles are required because the article is slotted into
  // <static-view-wrapper>, which means it lives in the light DOM —
  // ::slotted() can't style descendants. Keep these in lockstep with the
  // .text-section styles in views/public/static-view.ts.
  const styledArticle = `<article class="container py-5">
    <style>
      article.container {
        font-size: 1.0625rem;
        line-height: 1.75;
        color: rgba(230, 237, 243, 0.9);
        font-feature-settings: 'liga', 'kern';
        text-rendering: optimizeLegibility;
        -webkit-font-smoothing: antialiased;
        word-wrap: break-word;
        overflow-wrap: break-word;
      }
      article.container h1 {
        font-size: clamp(2rem, 1.6rem + 1.6vw, 2.75rem);
        font-weight: 600;
        line-height: 1.15;
        letter-spacing: -0.02em;
        color: #e6edf3;
        margin: 0 0 1.25rem;
      }
      article.container h2 {
        font-size: clamp(1.5rem, 1.25rem + 1vw, 1.875rem);
        font-weight: 600;
        line-height: 1.25;
        letter-spacing: -0.015em;
        color: #e6edf3;
        margin: 3rem 0 1rem;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.08);
      }
      article.container h3 {
        font-size: 1.375rem;
        font-weight: 600;
        line-height: 1.3;
        letter-spacing: -0.01em;
        color: #e6edf3;
        margin: 2.25rem 0 0.75rem;
      }
      article.container h4 {
        font-size: 1.125rem;
        font-weight: 600;
        line-height: 1.35;
        color: #e6edf3;
        margin: 1.75rem 0 0.5rem;
      }
      article.container p {
        margin: 0 0 1.25em;
      }
      article.container p:last-child {
        margin-bottom: 0;
      }
      article.container strong {
        color: #e6edf3;
        font-weight: 600;
      }
      article.container em {
        font-style: italic;
      }
      article.container a {
        color: #58a6ff;
        text-decoration: underline;
        text-underline-offset: 2px;
        text-decoration-thickness: 1px;
        transition: color 0.15s ease;
      }
      article.container a:hover {
        color: #79b8ff;
        text-decoration-thickness: 2px;
      }
      article.container ul,
      article.container ol {
        margin: 0 0 1.25em;
        padding-left: 1.6em;
      }
      article.container li {
        margin-bottom: 0.4em;
      }
      article.container li > p {
        margin-bottom: 0.4em;
      }
      article.container ul ul,
      article.container ol ol,
      article.container ul ol,
      article.container ol ul {
        margin: 0.4em 0;
      }
      article.container ul li::marker {
        color: rgba(230, 237, 243, 0.45);
      }
      article.container code {
        font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo,
          Consolas, monospace;
        font-size: 0.9em;
        background: rgba(110, 118, 129, 0.18);
        color: #e6edf3;
        padding: 0.15em 0.4em;
        border-radius: 4px;
      }
      article.container pre {
        margin: 1.5em 0;
        padding: 1.1rem 1.25rem;
        background: #0d1117;
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        overflow-x: auto;
        line-height: 1.55;
        font-size: 0.9rem;
      }
      article.container pre code {
        padding: 0;
        background: transparent;
        font-size: inherit;
        color: inherit;
      }
      article.container blockquote {
        margin: 1.5em 0;
        padding: 0.5em 1.25em;
        border-left: 3px solid #58a6ff;
        background: rgba(88, 166, 255, 0.06);
        color: rgba(230, 237, 243, 0.85);
        border-radius: 0 6px 6px 0;
      }
      article.container blockquote > :first-child {
        margin-top: 0;
      }
      article.container blockquote > :last-child {
        margin-bottom: 0;
      }
      article.container table {
        width: 100%;
        margin: 1.75em 0;
        border-collapse: collapse;
        font-size: 0.95rem;
        line-height: 1.55;
        background: rgba(13, 17, 23, 0.5);
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        overflow: hidden;
        display: block;
        overflow-x: auto;
      }
      article.container thead {
        background: rgba(110, 118, 129, 0.12);
      }
      article.container th,
      article.container td {
        padding: 0.7em 1em;
        text-align: left;
        vertical-align: top;
        border-bottom: 1px solid rgba(230, 237, 243, 0.06);
      }
      article.container th {
        font-weight: 600;
        color: #e6edf3;
        white-space: nowrap;
      }
      article.container tr:last-child td {
        border-bottom: none;
      }
      article.container tr:hover td {
        background: rgba(88, 166, 255, 0.04);
      }
      article.container hr {
        border: none;
        border-top: 1px solid rgba(230, 237, 243, 0.08);
        margin: 2.5rem 0;
      }
      article.container img {
        max-width: 100%;
        height: auto;
        border-radius: 8px;
        margin: 1.5em 0;
      }
      /* Team section styles (used by /about) */
      article.container .team-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 1.5rem;
        margin: 2rem 0;
      }
      article.container .team-member {
        text-align: center;
        padding: 1.5rem;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(230, 237, 243, 0.08);
      }
      article.container .team-photo {
        width: 140px;
        height: 140px;
        border-radius: 50%;
        object-fit: cover;
        margin-bottom: 1rem;
        border: 3px solid #58a6ff;
      }
      article.container .team-member h4 {
        font-size: 1.125rem;
        font-weight: 600;
        margin: 0.5rem 0 0.25rem;
        color: #e6edf3;
      }
      article.container .team-member p {
        font-size: 0.95rem;
        line-height: 1.6;
        text-align: left;
      }
      @media (max-width: 640px) {
        article.container {
          font-size: 1rem;
          line-height: 1.7;
        }
        article.container h2 {
          margin-top: 2.25rem;
        }
        article.container table {
          font-size: 0.875rem;
        }
        article.container th,
        article.container td {
          padding: 0.55em 0.7em;
        }
      }
    </style>
    ${html}
  </article>`;

  return styledArticle;
}

// generatePrivacyContent removed - use loadMarkdownContent(brandKey, 'privacy') directly

function escapeHtml(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function escapeAttr(value: string | number | null | undefined): string {
  return escapeHtml(value);
}

function formatPlanPrice(plan: any): string {
  if (plan.price_label) return plan.price_label;
  const monthly = plan.price_monthly;
  const annually = plan.price_annually;
  if (
    monthly === 0 &&
    (annually === 0 || annually === null || annually === undefined)
  ) {
    return 'Free';
  }
  if (monthly === null || monthly === undefined) {
    return 'Custom';
  }
  if (annually !== null && annually !== undefined) {
    return `$${monthly} / user / month — or $${annually} / user / year`;
  }
  return `$${monthly} / user / month`;
}

/**
 * Generate SEO/crawler-friendly pricing HTML rendered into the light DOM as
 * slotted children of <public-pricing-view>. The Lit component projects this
 * content via `<slot>` until its interactive UI has hydrated.
 *
 * Output is kept as plain, crawlable HTML (<h1>, <h2>, <ul>, <a>), with one
 * named slot per plan and per FAQ so the component can also read the
 * structured data back into its JS state if needed.
 */
function generatePricingSlottedContent(config: BrandConfig): string {
  const pricing = config.landing?.pricing || ({} as any);
  const plans = (pricing.plans || []) as any[];
  const faqs = (pricing.faqs || []) as any[];
  const title = pricing.title || `Pricing - ${config.name}`;
  const lead = pricing.lead || 'Choose the plan that fits your team.';

  const planBlocks = plans
    .map((plan, idx) => {
      const featureItems = (plan.features || [])
        .map((f: string) => `<li>${escapeHtml(f)}</li>`)
        .join('\n            ');
      const price = formatPlanPrice(plan);
      const cta = plan.cta_text || 'Learn more';
      const ctaUrl = plan.cta_url || '/register';
      const isExternal =
        typeof ctaUrl === 'string' && ctaUrl.startsWith('http');
      const target = isExternal
        ? ' target="_blank" rel="noopener noreferrer"'
        : '';
      const description = plan.description
        ? `<p class="plan-description">${escapeHtml(plan.description)}</p>`
        : '';
      const badge = plan.badge
        ? `<span class="badge">${escapeHtml(plan.badge)}</span>`
        : '';

      return `
        <div slot="plan-${idx}"
             class="plan plan-${escapeAttr(plan.id)}${plan.highlight ? ' highlight' : ''}"
             data-plan-id="${escapeAttr(plan.id)}"
             data-plan-name="${escapeAttr(plan.name)}"
             data-price-monthly="${escapeAttr(plan.price_monthly)}"
             data-price-annually="${escapeAttr(plan.price_annually)}"
             data-price-label="${escapeAttr(plan.price_label || '')}"
             data-badge="${escapeAttr(plan.badge || '')}"
             data-highlight="${plan.highlight ? 'true' : 'false'}"
             data-cta-text="${escapeAttr(cta)}"
             data-cta-url="${escapeAttr(ctaUrl)}"
             data-description="${escapeAttr(plan.description || '')}"
             data-features="${escapeAttr((plan.features || []).join('|'))}">
          ${badge}
          <h2>${escapeHtml(plan.name)}</h2>
          <p class="price">${escapeHtml(price)}</p>
          ${description}
          <ul>
            ${featureItems}
          </ul>
          <a class="plan-cta" href="${escapeAttr(ctaUrl)}"${target}>${escapeHtml(cta)}</a>
        </div>`;
    })
    .join('\n');

  const faqBlocks = faqs
    .map(
      (faq, idx) => `
        <div slot="faq-${idx}"
             class="faq-item"
             data-q="${escapeAttr(faq.q)}"
             data-a="${escapeAttr(faq.a)}">
          <h3>${escapeHtml(faq.q)}</h3>
          <p>${escapeHtml(faq.a)}</p>
        </div>`
    )
    .join('\n');

  return `
    <article class="pricing-content">
      <header class="pricing-header">
        <h1>${escapeHtml(title)}</h1>
        <p class="lead">${escapeHtml(lead)}</p>
      </header>

      <section class="pricing-plans">
        ${planBlocks}
      </section>

      ${
        faqs.length > 0
          ? `
      <section class="pricing-faq">
        <h2>Frequently Asked Questions</h2>
        ${faqBlocks}
      </section>`
          : ''
      }
    </article>
  `;
}
