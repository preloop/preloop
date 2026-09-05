/**
 * Build-time-discovered static markdown pages for the public SPA.
 *
 * OSS only ships the markdown files that exist under `frontend/content/<brand>/`
 * (today: terms, privacy, whatis-mcp). EE adds pages by dropping files in
 * `preloop-ee/frontend/content/<brand>/` (about, regulation instruments,
 * resources). The Vite brand plugin injects the discovered list onto
 * `window.BRAND_CONFIG.static_markdown_pages`; lit-app registers routes
 * from that list instead of hardcoding slugs.
 */

import type { BrandRuntimeConfig, RegulationNavLink } from './brand-config';

/** Always routed on self-hosted builds when the markdown file exists. */
export const CORE_STATIC_MARKDOWN_SLUGS = [
  'privacy',
  'terms',
  'whatis-mcp',
] as const;

export type StaticMarkdownPage = {
  path: string;
  src: string;
};

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

export function isStaticMarkdownSlug(slug: string): boolean {
  return SLUG_PATTERN.test(slug);
}

/**
 * Self-hosted editions stay login-first: only core legal/docs pages.
 * SaaS (and missing edition, treated as SaaS) ships every discovered file.
 */
export function allowStaticMarkdownSlug(
  slug: string,
  edition: string | undefined
): boolean {
  if (edition === 'saas' || !edition) {
    return true;
  }
  return (CORE_STATIC_MARKDOWN_SLUGS as readonly string[]).includes(slug);
}

export function staticMarkdownPageForSlug(
  slug: string,
  prefix = ''
): StaticMarkdownPage {
  if (prefix) {
    return {
      path: `/${prefix}/${slug}`,
      src: `/content/${prefix}/${slug}.md`,
    };
  }
  return { path: `/${slug}`, src: `/content/${slug}.md` };
}

/** ` /content/dora.md` -> `dora`; `/content/resources/foo.md` -> `resources/foo`. */
export function markdownRelFromSrc(src: string): string {
  return src.replace(/^\/content\//, '').replace(/\.md$/, '');
}

/**
 * Pages the SPA should register. Prefers the build-injected file list;
 * falls back to `regulation_pages` so an older runtime config still
 * registers instrument pages.
 */
export function pagesFromRuntimeConfig(
  config: Pick<BrandRuntimeConfig, 'static_markdown_pages' | 'regulation_pages'>
): StaticMarkdownPage[] {
  if (config.static_markdown_pages && config.static_markdown_pages.length > 0) {
    return config.static_markdown_pages;
  }
  return regulationPagesToMarkdown(
    config.regulation_pages as RegulationNavLink[] | undefined
  );
}

function regulationPagesToMarkdown(
  links: RegulationNavLink[] | undefined
): StaticMarkdownPage[] {
  if (!links?.length) {
    return [];
  }
  return links.map((link) => ({
    path: link.href,
    src: `/content${link.href}.md`,
  }));
}
