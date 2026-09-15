import { expect, fixture, html } from '@open-wc/testing';
import { setViewport } from '@web/test-runner-commands';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import sinon from 'sinon';

import './static-view-wrapper';
import '../views/public/static-view';
import { ARTICLE_STYLES } from '../article-styles';
import { render_blog_post_html, type BlogPost } from '../blog-seo';

// The blog article is slotted into <static-view-wrapper> on first load and
// fetched into <static-view> on client-side navigation. Both shells size
// their <main> the same way, so both are measured here at phone widths: a
// reading column that is wider than the viewport clips the right edge of
// every line on a phone, whatever the post contains.

const BRAND_CONFIG: Record<string, unknown> = {
  name: 'Test Brand',
  domain: 'test.example.com',
  edition: 'saas',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
  branding: {
    logo_light: '/logo.svg',
    logo_dark: '/logo-dark.svg',
    favicon: '/favicon.ico',
    primary_color: '#000',
    gradient_product: '',
    gradient_ai: '',
  },
  social: { twitter: '', linkedin: '', instagram: '' },
};

const SAMPLE_POST: BlogPost = {
  slug: 'sample-post',
  title: 'A sample post with a fairly long headline that wraps on a phone',
  description: 'Sample description.',
  date: '2026-07-18',
  author: 'Jane Doe',
  tags: ['governance', 'audit trail'],
  og_image: '',
  related: ['/pricing'],
  reading_minutes: 4,
  body_html:
    '<p>Body copy that is long enough to wrap onto several lines at a ' +
    'phone width so the right edge of the reading column is exercised.</p>' +
    '<pre><code>preloop agents onboard --name example --url https://example.com/very/long/path/that/does/not/wrap</code></pre>' +
    '<p><img src="data:image/svg+xml;utf8,' +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="300"></svg>'
    ) +
    '" alt=""></p>',
};

// The same fragment the build writes to content/blog/<slug>.html, styles
// included.
const ARTICLE_HTML = render_blog_post_html(
  SAMPLE_POST,
  BRAND_CONFIG as never,
  ARTICLE_STYLES
);

const PHONE_WIDTHS = [360, 390, 414];

function stubFetch(body: string): sinon.SinonStub {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      }
      if (url.endsWith('.html')) {
        return new Response(body, {
          status: 200,
          headers: { 'Content-Type': 'text/html' },
        });
      }
      return new Response('{}', { status: 200 });
    });
}

/**
 * Right-most visible edge of `el` and everything inside it, in viewport
 * pixels. Children of an element that scrolls or clips horizontally (a code
 * block, a wide table) are not descended into: what they overflow is their
 * own scroll container, not the page.
 */
function rightEdge(el: Element): number {
  let right = el.getBoundingClientRect().right;
  const overflowX = getComputedStyle(el).overflowX;
  if (
    overflowX === 'auto' ||
    overflowX === 'scroll' ||
    overflowX === 'hidden'
  ) {
    return right;
  }
  for (const child of el.children) {
    if (child.getBoundingClientRect().width > 0) {
      right = Math.max(right, rightEdge(child));
    }
  }
  return right;
}

function shellMain(host: HTMLElement): HTMLElement {
  return host.shadowRoot!.querySelector('main') as HTMLElement;
}

async function waitForArticle(host: HTMLElement): Promise<HTMLElement> {
  for (let i = 0; i < 50; i += 1) {
    const article =
      host.querySelector('article.blog-post') ||
      host.shadowRoot?.querySelector('article.blog-post');
    if (article) {
      return article as HTMLElement;
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('article.blog-post never rendered');
}

async function settle(): Promise<void> {
  await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  await new Promise((resolve) => setTimeout(resolve, 50));
}

describe('blog article shells at phone widths', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as unknown as { BRAND_CONFIG?: unknown }).BRAND_CONFIG =
      BRAND_CONFIG;
    fetchStub = stubFetch(ARTICLE_HTML);
  });

  afterEach(async () => {
    fetchStub.restore();
    delete (window as unknown as { BRAND_CONFIG?: unknown }).BRAND_CONFIG;
    await setViewport({ width: 1280, height: 800 });
  });

  for (const width of PHONE_WIDTHS) {
    it(`static-view-wrapper keeps the slotted article inside a ${width}px viewport`, async () => {
      await setViewport({ width, height: 844 });
      const host = (await fixture(
        html`<static-view-wrapper
          >${unsafeHTML(ARTICLE_HTML)}</static-view-wrapper
        >`
      )) as HTMLElement;
      const article = await waitForArticle(host);
      await settle();

      const main = shellMain(host);
      expect(
        main.getBoundingClientRect().right,
        'main right edge'
      ).to.be.at.most(window.innerWidth);
      expect(rightEdge(article), 'article right edge').to.be.at.most(
        window.innerWidth
      );
      expect(
        document.documentElement.scrollWidth,
        'document scroll width'
      ).to.equal(window.innerWidth);
    });

    it(`static-view keeps the fetched article inside a ${width}px viewport`, async () => {
      await setViewport({ width, height: 844 });
      const host = (await fixture(
        html`<static-view src="/content/blog/sample-post.html"></static-view>`
      )) as HTMLElement;
      const article = await waitForArticle(host);
      await settle();

      const main = shellMain(host);
      expect(
        main.getBoundingClientRect().right,
        'main right edge'
      ).to.be.at.most(window.innerWidth);
      expect(rightEdge(article), 'article right edge').to.be.at.most(
        window.innerWidth
      );
      expect(
        document.documentElement.scrollWidth,
        'document scroll width'
      ).to.equal(window.innerWidth);
    });
  }

  it('keeps the 760px reading column on a desktop viewport', async () => {
    await setViewport({ width: 1280, height: 800 });
    const host = (await fixture(
      html`<static-view-wrapper
        >${unsafeHTML(ARTICLE_HTML)}</static-view-wrapper
      >`
    )) as HTMLElement;
    const article = await waitForArticle(host);
    await settle();

    const main = shellMain(host);
    const style = getComputedStyle(main);
    const contentWidth =
      main.clientWidth -
      parseFloat(style.paddingLeft) -
      parseFloat(style.paddingRight);
    expect(contentWidth, 'reading column width').to.equal(760);
    expect(article.getBoundingClientRect().width).to.equal(760);
  });
});
