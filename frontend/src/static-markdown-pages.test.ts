import { expect } from '@open-wc/testing';

import {
  allowStaticMarkdownSlug,
  markdownRelFromSrc,
  pagesFromRuntimeConfig,
  staticMarkdownPageForSlug,
} from './static-markdown-pages';

describe('static-markdown-pages', () => {
  it('keeps only core slugs on self-hosted builds', () => {
    expect(allowStaticMarkdownSlug('privacy', 'selfhosted')).to.equal(true);
    expect(allowStaticMarkdownSlug('dora', 'selfhosted')).to.equal(false);
    expect(allowStaticMarkdownSlug('about', 'selfhosted')).to.equal(false);
  });

  it('allows every slug on SaaS builds', () => {
    expect(allowStaticMarkdownSlug('dora', 'saas')).to.equal(true);
    expect(allowStaticMarkdownSlug('about', 'saas')).to.equal(true);
  });

  it('maps a slug to /content/<slug>.md', () => {
    expect(staticMarkdownPageForSlug('dora')).to.deep.equal({
      path: '/dora',
      src: '/content/dora.md',
    });
    expect(staticMarkdownPageForSlug('pillar', 'resources')).to.deep.equal({
      path: '/resources/pillar',
      src: '/content/resources/pillar.md',
    });
    expect(markdownRelFromSrc('/content/dora.md')).to.equal('dora');
    expect(markdownRelFromSrc('/content/resources/pillar.md')).to.equal(
      'resources/pillar'
    );
  });

  it('prefers the injected file list over regulation_pages', () => {
    const pages = pagesFromRuntimeConfig({
      static_markdown_pages: [{ path: '/terms', src: '/content/terms.md' }],
      regulation_pages: [{ href: '/dora', label: 'DORA' }],
    });
    expect(pages).to.deep.equal([{ path: '/terms', src: '/content/terms.md' }]);
  });

  it('falls back to regulation_pages when the file list is absent', () => {
    const pages = pagesFromRuntimeConfig({
      regulation_pages: [{ href: '/nis2', label: 'NIS2' }],
    });
    expect(pages).to.deep.equal([{ path: '/nis2', src: '/content/nis2.md' }]);
  });

  it('registers no extra pages when the runtime config is empty', () => {
    expect(pagesFromRuntimeConfig({})).to.deep.equal([]);
  });
});
