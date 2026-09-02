import type { BrandConfig } from './brand-config';
import type { RouteMeta } from './brand-seo';

/**
 * Blog support for `/blog` (index) and `/blog/<slug>` (posts).
 *
 * Design notes
 * ------------
 * Posts are markdown files with YAML frontmatter under
 * `content/<brand>/blog/<slug>.md`. Unlike `/vs/<slug>` pages — which carry
 * their metadata in the `VS_PAGE_META` registry in `brand-seo.ts` — a blog
 * grows continuously, so requiring a TypeScript edit per post would make
 * every post a code change. Frontmatter keeps one post in one file and one
 * commit.
 *
 * This module is deliberately free of `fs` and `js-yaml` imports so it stays
 * safe to pull into the browser bundle and unit-testable in the web test
 * runner. The vite plugin does the filesystem walk and the YAML parse, then
 * hands plain `BlogPost` objects to the pure functions here.
 *
 * The blog is SaaS-only (see `is_blog_enabled`). A self-hosted OSS or
 * Enterprise install serves its own landing page out of this same bundle;
 * shipping Preloop's marketing blog onto a customer's internal instance would
 * repeat the D23 leak (OSS install instructions surfacing on self-hosted
 * landing pages), and every canonical URL here is rooted at `config.domain`,
 * so a self-hosted build would emit canonicals pointing at preloop.ai from
 * someone else's hostname.
 */

export const BLOG_BASE_PATH = '/blog';

/** Slug charset accepted from disk and from the router. */
export const BLOG_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

/**
 * A single blog post. `body_html` is the rendered markdown body and is only
 * populated by the build; the pure helpers here never need it except when
 * generating the feed.
 */
export type BlogPost = {
  /** URL slug, derived from the filename. */
  slug: string;
  /** Post title. Also the `<h1>` and the schema.org `headline`. */
  title: string;
  /** Meta description and feed summary. Keep under ~160 characters. */
  description: string;
  /** ISO date (YYYY-MM-DD) the post was first published. */
  date: string;
  /** ISO date the post was last materially revised, if ever. */
  updated?: string;
  /** Display name of the author. Posts are founder-signed, not corporate. */
  author?: string;
  /** Optional profile URL for the author. */
  author_url?: string;
  /** Free-form topic tags, surfaced in the index and in `about`. */
  tags?: string[];
  /** Route-specific social image. Falls back to the brand default. */
  og_image?: string;
  /**
   * Internal routes this post should link to (e.g. `/vs/litellm`). Rendered as
   * a "Related" block so link equity flows into the pages that already rank.
   */
  related?: string[];
  /** Estimated reading time in minutes, computed at build time. */
  reading_minutes?: number;
  /** When true the post is skipped by the build entirely. */
  draft?: boolean;
  /** Rendered HTML body (build-time only). */
  body_html?: string;
};

export type BlogPostMetaInput = Omit<BlogPost, 'body_html'>;

/** The blog only ships on SaaS builds. */
export function is_blog_enabled(config: BrandConfig): boolean {
  return (config.edition || 'saas') === 'saas';
}

export function get_blog_route(slug: string): string {
  return `${BLOG_BASE_PATH}/${slug}`;
}

/** Extract `<slug>` from `/blog/<slug>`, or null if the route is not a post. */
export function get_blog_slug_from_route(route: string): string | null {
  if (!route.startsWith(`${BLOG_BASE_PATH}/`)) {
    return null;
  }
  const slug = route.slice(BLOG_BASE_PATH.length + 1);
  if (!slug || slug.includes('/') || !BLOG_SLUG_PATTERN.test(slug)) {
    return null;
  }
  return slug;
}

/** Newest first. Ties broken by slug so the ordering is deterministic. */
export function sort_posts(posts: BlogPost[]): BlogPost[] {
  return [...posts].sort((a, b) => {
    if (a.date === b.date) {
      return a.slug.localeCompare(b.slug);
    }
    return a.date < b.date ? 1 : -1;
  });
}

export function get_published_posts(posts: BlogPost[]): BlogPost[] {
  return sort_posts(posts.filter((post) => !post.draft));
}

/**
 * Routes contributed to the sitemap and llms.txt: the index plus one entry per
 * published post. Empty for non-SaaS editions.
 */
export function get_blog_routes(
  config: BrandConfig,
  posts: BlogPost[]
): string[] {
  if (!is_blog_enabled(config)) {
    return [];
  }
  const published = get_published_posts(posts);
  if (published.length === 0) {
    return [];
  }
  return [
    BLOG_BASE_PATH,
    ...published.map((post) => get_blog_route(post.slug)),
  ];
}

// -----------------------------------------------------------------------------
// Formatting helpers
// -----------------------------------------------------------------------------

export function escape_html(value: string): string {
  // Regex replacement rather than String.replaceAll: this module is compiled
  // against the frontend's tsconfig lib, which predates ES2021.
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** "2026-07-18" -> "18 July 2026". Invalid input passes through unchanged. */
export function format_display_date(iso_date: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso_date || '');
  if (!match) {
    return iso_date || '';
  }
  const months = [
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December',
  ];
  const month = months[Number(match[2]) - 1];
  if (!month) {
    return iso_date;
  }
  return `${Number(match[3])} ${month} ${match[1]}`;
}

/** RFC 822 date for RSS `<pubDate>`. Dates are treated as midnight UTC. */
export function to_rfc822(iso_date: string): string {
  const parsed = new Date(`${iso_date}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }
  return parsed.toUTCString();
}

/** ~200 words per minute, floor of 1. */
export function estimate_reading_minutes(markdown_body: string): number {
  const words = (markdown_body || '').trim().split(/\s+/).filter(Boolean);
  return Math.max(1, Math.round(words.length / 200));
}

/**
 * Compare headings without markup. Walks the string so an unclosed `<`
 * cannot leave a residual `<script` the way a single `/<[^>]+>/` replace
 * would. The result is only compared, never written back into HTML.
 */
function normalize_heading_text(value: string): string {
  let out = '';
  let in_tag = false;
  for (const ch of value) {
    if (ch === '<') {
      in_tag = true;
      continue;
    }
    if (ch === '>') {
      in_tag = false;
      continue;
    }
    if (!in_tag) {
      out += ch;
    }
  }
  return out.replace(/\s+/g, ' ').trim().toLowerCase();
}

/**
 * Drop a leading `# Title` that repeats frontmatter `title`. Authors keep a
 * standalone markdown heading; the page template already emits `<h1>`.
 */
export function strip_leading_markdown_title(
  markdown: string,
  title: string
): string {
  const match = /^(?:\uFEFF)?(?:[ \t]*\r?\n)*#\s+(.+?)\s*(?:\r?\n)+/.exec(
    markdown || ''
  );
  if (!match) {
    return markdown;
  }
  if (normalize_heading_text(match[1]) !== normalize_heading_text(title)) {
    return markdown;
  }
  return (markdown || '').slice(match[0].length);
}

/**
 * Drop a leading `<h1>` from rendered markdown when it repeats `title`.
 */
export function strip_duplicate_title_heading(
  html: string,
  title: string
): string {
  const source = html || '';
  const match = /^\s*<h1(?:\s[^>]*)?>([\s\S]*?)<\/h1>\s*/i.exec(source);
  if (!match) {
    return source;
  }
  if (normalize_heading_text(match[1]) !== normalize_heading_text(title)) {
    return source;
  }
  return source.slice(match[0].length);
}

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
  return `${get_origin(config)}${path.startsWith('/') ? '' : '/'}${path}`;
}

function get_default_author(config: BrandConfig): string {
  return config.company?.legal_name || config.name || 'Preloop';
}

// -----------------------------------------------------------------------------
// Route metadata
// -----------------------------------------------------------------------------

export function get_blog_index_meta(config: BrandConfig): RouteMeta {
  const default_og_image = config.landing?.meta?.og_image || '';
  const title = `Blog — Engineering notes on governing AI agents | ${config.name}`;
  const description = `Engineering write-ups from building ${config.name}, the open-source AI agent control plane: real bugs, real diagnoses, and how MCP firewalls, AI model gateways, and human approvals behave in practice.`;
  return {
    title,
    description,
    keywords: `${config.name} blog, AI agent control plane engineering, MCP firewall, AI model gateway, human in the loop approvals, AI agent governance, open source AI agent platform, engineering postmortem`,
    og_image: default_og_image,
    og_title: title,
    og_description: description,
  };
}

export function get_blog_post_meta(
  post: BlogPostMetaInput,
  config: BrandConfig
): RouteMeta {
  const default_keywords = config.landing?.meta?.keywords || '';
  const tag_keywords = (post.tags || []).join(', ');
  const keywords = [tag_keywords, default_keywords]
    .filter((value) => value && value.trim().length > 0)
    .join(', ');
  const title = `${post.title} | ${config.name}`;
  return {
    title,
    description: post.description,
    keywords,
    og_image: post.og_image || config.landing?.meta?.og_image || '',
    og_title: post.title,
    og_description: post.description,
  };
}

// -----------------------------------------------------------------------------
// Structured data
// -----------------------------------------------------------------------------

type SchemaObject = Record<string, unknown>;

function get_organization_id(config: BrandConfig): string {
  return `${get_origin(config)}/#organization`;
}

function get_website_id(config: BrandConfig): string {
  return `${get_origin(config)}/#website`;
}

function build_author_schema(
  post: BlogPostMetaInput,
  config: BrandConfig
): SchemaObject {
  const name = post.author || get_default_author(config);
  const author: SchemaObject = { '@type': 'Person', name };
  if (post.author_url) {
    author.url = post.author_url;
  }
  return author;
}

/**
 * `BlogPosting` for a single post. Answer engines lean on this far harder than
 * classic search does, so every field that can be filled honestly is filled:
 * author as a real `Person`, both publish and modify dates, word count, and
 * the `about` topics drawn from the post's own tags.
 */
export function buildBlogPostingSchema(
  post: BlogPostMetaInput,
  config: BrandConfig
): SchemaObject {
  const route = get_blog_route(post.slug);
  const url = `${get_origin(config)}${route}`;
  const image = post.og_image || config.landing?.meta?.og_image || '';

  const schema: SchemaObject = {
    '@context': 'https://schema.org',
    '@type': 'BlogPosting',
    '@id': `${url}#post`,
    headline: post.title,
    description: post.description,
    url,
    mainEntityOfPage: { '@type': 'WebPage', '@id': url },
    datePublished: post.date,
    dateModified: post.updated || post.date,
    author: build_author_schema(post, config),
    publisher: { '@id': get_organization_id(config) },
    isPartOf: { '@id': `${get_origin(config)}${BLOG_BASE_PATH}#blog` },
    inLanguage: 'en',
    isAccessibleForFree: true,
  };

  if (image) {
    schema.image = absolute_url(image, config);
  }
  if (post.tags && post.tags.length > 0) {
    schema.keywords = post.tags.join(', ');
    schema.about = post.tags.map((tag) => ({ '@type': 'Thing', name: tag }));
  }
  return schema;
}

/** `Blog` + `ItemList` for the index page. */
export function buildBlogSchema(
  config: BrandConfig,
  posts: BlogPost[]
): SchemaObject {
  const origin = get_origin(config);
  const published = get_published_posts(posts);
  const meta = get_blog_index_meta(config);

  return {
    '@context': 'https://schema.org',
    '@type': 'Blog',
    '@id': `${origin}${BLOG_BASE_PATH}#blog`,
    url: `${origin}${BLOG_BASE_PATH}`,
    name: `${config.name} Blog`,
    description: meta.description,
    publisher: { '@id': get_organization_id(config) },
    isPartOf: { '@id': get_website_id(config) },
    inLanguage: 'en',
    blogPost: published.map((post) => ({
      '@type': 'BlogPosting',
      '@id': `${origin}${get_blog_route(post.slug)}#post`,
      headline: post.title,
      description: post.description,
      url: `${origin}${get_blog_route(post.slug)}`,
      datePublished: post.date,
      dateModified: post.updated || post.date,
      author: build_author_schema(post, config),
    })),
  };
}

/**
 * `BreadcrumbList` so answer engines can place a post in the site hierarchy
 * (Home > Blog > Post) rather than treating it as an orphan URL.
 */
export function buildBlogBreadcrumbSchema(
  post: BlogPostMetaInput,
  config: BrandConfig
): SchemaObject {
  const origin = get_origin(config);
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: [
      {
        '@type': 'ListItem',
        position: 1,
        name: config.name,
        item: `${origin}/`,
      },
      {
        '@type': 'ListItem',
        position: 2,
        name: 'Blog',
        item: `${origin}${BLOG_BASE_PATH}`,
      },
      {
        '@type': 'ListItem',
        position: 3,
        name: post.title,
        item: `${origin}${get_blog_route(post.slug)}`,
      },
    ],
  };
}

// -----------------------------------------------------------------------------
// Rendered fragments
// -----------------------------------------------------------------------------

/**
 * Shared long-form article styles. These are emitted as a light-DOM `<style>`
 * tag next to the article because the content is slotted into
 * `<static-view-wrapper>` and `::slotted()` cannot reach descendants. This
 * mirrors `loadMarkdownContent` in `vite-plugin-brand.ts` — keep the two in
 * lockstep with `.text-section` in `views/public/static-view.ts`.
 *
 * Values come straight from DESIGN.md: `#e6edf3` primary text, `#30C9E8`
 * info-cyan used semantically for the dateline rule only, 8px spacing base,
 * 4px/8px radii, no glass and no glow.
 */
export const BLOG_ARTICLE_STYLES = `
      .blog-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem 1rem;
        align-items: baseline;
        margin: 0 0 2rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid rgba(230, 237, 243, 0.08);
        font-size: 0.9375rem;
        color: rgba(230, 237, 243, 0.6);
      }
      .blog-meta .blog-author {
        color: #e6edf3;
        font-weight: 500;
      }
      .blog-meta .blog-sep {
        color: rgba(230, 237, 243, 0.3);
      }
      .blog-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin: 0 0 2rem;
        padding: 0;
        list-style: none;
      }
      .blog-tags li {
        margin: 0;
        padding: 0.15rem 0.6rem;
        border: 1px solid rgba(230, 237, 243, 0.12);
        border-radius: 4px;
        font-size: 0.8125rem;
        color: rgba(230, 237, 243, 0.7);
      }
      .blog-related {
        margin: 3rem 0 0;
        padding: 1.25rem 1.5rem;
        border: 1px solid rgba(230, 237, 243, 0.08);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.02);
      }
      .blog-related h2 {
        font-size: 1.0625rem;
        font-weight: 600;
        margin: 0 0 0.75rem;
        padding: 0;
        border: none;
        letter-spacing: 0;
      }
      .blog-related ul {
        margin: 0;
        padding-left: 1.2em;
      }
      .blog-backlink {
        display: inline-block;
        margin: 0 0 1.5rem;
        font-size: 0.9375rem;
        color: rgba(230, 237, 243, 0.6);
        text-decoration: none;
      }
      .blog-backlink:hover {
        color: #e6edf3;
      }
      .blog-hero {
        margin: 0 0 2rem;
        padding: 0;
        border: none;
      }
      .blog-hero img {
        display: block;
        width: 100%;
        height: auto;
        border-radius: 4px;
      }
      .blog-index-hero {
        display: block;
        margin: 0.75rem 0 0;
      }
      .blog-index-hero img {
        display: block;
        width: 100%;
        height: auto;
        border-radius: 4px;
      }
      .blog-index-list {
        list-style: none;
        margin: 2.5rem 0 0;
        padding: 0;
      }
      .blog-index-list > li {
        margin: 0;
        padding: 2rem 0;
        border-top: 1px solid rgba(230, 237, 243, 0.08);
      }
      .blog-index-list > li:last-child {
        border-bottom: 1px solid rgba(230, 237, 243, 0.08);
      }
      .blog-index-list h2 {
        font-size: 1.5rem;
        font-weight: 600;
        line-height: 1.3;
        letter-spacing: -0.015em;
        margin: 0 0 0.5rem;
        padding: 0;
        border: none;
      }
      .blog-index-list h2 a {
        color: #e6edf3;
        text-decoration: none;
      }
      .blog-index-list h2 a:hover {
        color: #30c9e8;
      }
      .blog-index-list p {
        margin: 0.5rem 0 0;
        color: rgba(230, 237, 243, 0.75);
      }
      .blog-index-dateline {
        font-size: 0.875rem;
        color: rgba(230, 237, 243, 0.55);
      }
      .blog-feed-link {
        display: inline-block;
        margin-top: 2.5rem;
        font-size: 0.9375rem;
      }
`;

/** Human-readable label for an internal route used in the "Related" block. */
export function get_internal_link_label(route: string): string {
  if (route === '/pricing') return 'Pricing';
  if (route === '/about') return 'About';
  if (route === '/whatis-mcp') return 'What is MCP?';
  if (route === '/ai-act-readiness') return 'EU AI Act readiness';
  if (route === '/cra-readiness') return 'Cyber Resilience Act';
  if (route === '/dora') return 'DORA';
  if (route === '/nis2') return 'NIS2';
  if (route.startsWith('/vs/')) {
    const slug = route.slice('/vs/'.length);
    const name = slug
      .split('-')
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
    return `Preloop vs ${name}`;
  }
  if (route.startsWith('/resources/')) {
    const slug = route.slice('/resources/'.length);
    return slug
      .split('-')
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  }
  if (route.startsWith(`${BLOG_BASE_PATH}/`)) {
    return route.slice(BLOG_BASE_PATH.length + 1).replace(/-/g, ' ');
  }
  return route;
}

function render_related_block(post: BlogPost): string {
  const related = (post.related || []).filter(Boolean);
  if (related.length === 0) {
    return '';
  }
  const items = related
    .map(
      (route) =>
        `<li><a href="${escape_html(route)}">${escape_html(
          get_internal_link_label(route)
        )}</a></li>`
    )
    .join('\n        ');
  return `
    <aside class="blog-related">
      <h2>Related reading</h2>
      <ul>
        ${items}
      </ul>
    </aside>`;
}

/**
 * The full light-DOM article for a single post, ready to be slotted into
 * `<static-view-wrapper>`.
 */
export function render_blog_post_html(
  post: BlogPost,
  config: BrandConfig,
  article_styles: string
): string {
  const author = escape_html(post.author || get_default_author(config));
  const author_html = post.author_url
    ? `<a href="${escape_html(post.author_url)}" rel="author">${author}</a>`
    : author;
  const tags = (post.tags || []).filter(Boolean);
  const tags_html =
    tags.length > 0
      ? `<ul class="blog-tags">${tags
          .map((tag) => `<li>${escape_html(tag)}</li>`)
          .join('')}</ul>`
      : '';
  const updated_html =
    post.updated && post.updated !== post.date
      ? `<span class="blog-sep">·</span><span>Updated <time datetime="${escape_html(
          post.updated
        )}">${escape_html(format_display_date(post.updated))}</time></span>`
      : '';
  const reading_html = post.reading_minutes
    ? `<span class="blog-sep">·</span><span>${post.reading_minutes} min read</span>`
    : '';
  const hero_html = post.og_image
    ? `<figure class="blog-hero"><img src="${escape_html(
        post.og_image
      )}" alt=""></figure>`
    : '';
  const body_html = strip_duplicate_title_heading(
    post.body_html || '',
    post.title
  );

  return `<article class="container py-5 blog-post">
    <style>${article_styles}${BLOG_ARTICLE_STYLES}</style>
    <a class="blog-backlink" href="${BLOG_BASE_PATH}">&larr; All posts</a>
    <h1>${escape_html(post.title)}</h1>
    <div class="blog-meta">
      <span class="blog-author">${author_html}</span>
      <span class="blog-sep">·</span>
      <span><time datetime="${escape_html(post.date)}">${escape_html(
        format_display_date(post.date)
      )}</time></span>
      ${updated_html}
      ${reading_html}
    </div>
    ${tags_html}
    ${hero_html}
    ${body_html}
    ${render_related_block(post)}
  </article>`;
}

/** The `/blog` index: a dated list of every published post. */
export function render_blog_index_html(
  config: BrandConfig,
  posts: BlogPost[],
  article_styles: string
): string {
  const published = get_published_posts(posts);
  const intro = `Engineering notes from building ${escape_html(
    config.name
  )} — the open-source AI agent control plane. Mostly real bugs with real diagnoses, plus the occasional reference piece on how agent governance actually fits together.`;

  const items =
    published.length > 0
      ? published
          .map((post) => {
            const route = get_blog_route(post.slug);
            const reading = post.reading_minutes
              ? ` · ${post.reading_minutes} min read`
              : '';
            const hero = post.og_image
              ? `<a class="blog-index-hero" href="${escape_html(
                  route
                )}"><img src="${escape_html(post.og_image)}" alt=""></a>`
              : '';
            return `      <li>
        <p class="blog-index-dateline"><time datetime="${escape_html(
          post.date
        )}">${escape_html(format_display_date(post.date))}</time>${reading}</p>
        ${hero}
        <h2><a href="${escape_html(route)}">${escape_html(post.title)}</a></h2>
        <p>${escape_html(post.description)}</p>
      </li>`;
          })
          .join('\n')
      : '      <li><p>No posts yet.</p></li>';

  return `<article class="container py-5 blog-index">
    <style>${article_styles}${BLOG_ARTICLE_STYLES}</style>
    <h1>Blog</h1>
    <p>${intro}</p>
    <ul class="blog-index-list">
${items}
    </ul>
    <p class="blog-feed-link"><a href="${BLOG_BASE_PATH}/feed.xml">RSS feed</a></p>
  </article>`;
}

// -----------------------------------------------------------------------------
// Feed
// -----------------------------------------------------------------------------

function escape_xml(value: string): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

/**
 * RSS 2.0 feed at `/blog/feed.xml`. RSS rather than Atom because every reader
 * and every aggregator consumes it, and the `atom:link rel="self"` element
 * gives it the self-reference Atom would otherwise be needed for.
 */
export function generate_blog_feed_xml(
  config: BrandConfig,
  posts: BlogPost[]
): string {
  const origin = get_origin(config);
  const published = get_published_posts(posts);
  const meta = get_blog_index_meta(config);
  const last_build = published[0]?.date
    ? to_rfc822(published[0].date)
    : to_rfc822(new Date().toISOString().slice(0, 10));

  const items = published
    .map((post) => {
      const url = `${origin}${get_blog_route(post.slug)}`;
      const categories = (post.tags || [])
        .map((tag) => `      <category>${escape_xml(tag)}</category>`)
        .join('\n');
      return `    <item>
      <title>${escape_xml(post.title)}</title>
      <link>${escape_xml(url)}</link>
      <guid isPermaLink="true">${escape_xml(url)}</guid>
      <pubDate>${escape_xml(to_rfc822(post.date))}</pubDate>
      <description>${escape_xml(post.description)}</description>
      <dc:creator>${escape_xml(
        post.author || get_default_author(config)
      )}</dc:creator>
${categories}
    </item>`;
    })
    .join('\n');

  return `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>${escape_xml(config.name)} Blog</title>
    <link>${escape_xml(`${origin}${BLOG_BASE_PATH}`)}</link>
    <description>${escape_xml(meta.description)}</description>
    <language>en</language>
    <lastBuildDate>${escape_xml(last_build)}</lastBuildDate>
    <atom:link href="${escape_xml(
      `${origin}${BLOG_BASE_PATH}/feed.xml`
    )}" rel="self" type="application/rss+xml" />
${items}
  </channel>
</rss>
`;
}

/**
 * The blog section of `llms.txt`. Answer engines get title, date and summary
 * per post rather than a bare URL list, which is what the rest of the file
 * emits for navigational pages.
 */
export function generate_blog_llms_section(
  config: BrandConfig,
  posts: BlogPost[]
): string[] {
  if (!is_blog_enabled(config)) {
    return [];
  }
  const published = get_published_posts(posts);
  if (published.length === 0) {
    return [];
  }
  const origin = get_origin(config);
  return [
    'Blog posts (engineering write-ups, newest first):',
    ...published.map(
      (post) =>
        `- ${post.title} (${post.date}) -> ${origin}${get_blog_route(
          post.slug
        )} — ${post.description}`
    ),
    `- RSS feed -> ${origin}${BLOG_BASE_PATH}/feed.xml`,
    '',
  ];
}
